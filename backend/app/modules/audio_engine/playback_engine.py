"""Playback coordination — the core of the Reading Audio Engine.

Owns the loop that takes a sentence off the queue, speaks it, advances the
pointer, and repeats. Everything else it delegates: state to the state machine,
position to the pointer manager, pending text to the queue, and speech to a
provider.

It never touches OCR, the camera, Google Vision, or the AI Engine. It answers
one question: given the current pointer, what should I speak and how?

Two invariants drive the design:

1. A sentence is never cut off mid-utterance by a pointer change. A Reading
   Update moves to WAITING_FOR_POINTER and the jump lands after the current
   sentence finishes. Meaning Mode is the deliberate exception — it stops the
   sink immediately, because a reader who does not understand a word should not
   have to wait.
2. Merge Memory updates never restart the sentence in flight. It has already
   been dequeued, so a refresh only rewrites what has not been spoken.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from backend.app.modules.audio_engine.audio_profiles import get_profile
from backend.app.modules.audio_engine.models import (
    AmbientState,
    AudioProfile,
    AudioRuntimeState,
    PauseReason,
    PlaybackState,
    PlaybackStatistics,
    PlaybackStatus,
    ReadingPointer,
    SentenceChunk,
    SpeechRequest,
    Voice,
)
from backend.app.modules.audio_engine.pointer_manager import PointerManager
from backend.app.modules.audio_engine.sentence_queue import SentenceQueue, segment_sentences
from backend.app.modules.audio_engine.interfaces import AmbientProviderInterface
from backend.app.modules.audio_engine.scene_controller import SceneController
from backend.app.modules.audio_engine.speech_provider import (
    AudioSink,
    NullAudioSink,
    get_provider,
)
from backend.app.modules.audio_engine.state_machine import PlaybackStateMachine

logger = logging.getLogger(__name__)


class _Counters:
    """Mutable playback tallies.

    Kept beside the engine rather than in a statistics module so there is one
    clock and one set of counts. A separate timer would drift from the state
    machine's, and the two would eventually disagree.
    """

    def __init__(self) -> None:
        self.sentences = 0
        self.words = 0
        self.characters = 0
        self.pauses = 0
        self.meaning_mode = 0
        self.reading_updates = 0
        self.queue_refreshes = 0
        self.stale_rejected = 0
        self.pages: set[int] = set()
        self.started_at: float | None = None
        # Reading time from pages already turned. The state machine's clock is
        # zeroed by the IDLE/READY transitions a page turn goes through, so
        # without this the session's reading time would restart at every page.
        self.carried_reading_ms = 0

    def reset(self) -> None:
        self.__init__()


class PlaybackEngine:
    """Implements PlaybackEngineInterface."""

    def __init__(
        self,
        *,
        provider=None,
        sink: AudioSink | None = None,
        state_machine: PlaybackStateMachine | None = None,
        auto_advance: bool = True,
        session_id: str = "default",
        clock: Callable[[], float] = time.monotonic,
        ambient_provider: AmbientProviderInterface | None = None,
        scene_controller: SceneController | None = None,
    ) -> None:
        # `auto_advance=False` lets tests drive the loop one sentence at a time.
        self._provider = provider if provider is not None else get_provider()
        self._sink = sink if sink is not None else NullAudioSink()
        self._machine = state_machine or PlaybackStateMachine(clock=clock)
        self._pointer = PointerManager()
        self._queue = SentenceQueue()
        self._auto_advance = auto_advance
        self._session_id = session_id
        self._clock = clock
        
        self._ambient = ambient_provider
        self._scene = scene_controller
        self._audio_generation = 0
        self._ambient_was_active = False
        self._tts_was_active = False

        self._profile: AudioProfile = get_profile(None)
        self._voice_id: str | None = None
        self._current: SentenceChunk | None = None
        self._pending_seek: ReadingPointer | None = None
        self._error: str | None = None

        self._stats = _Counters()
        # Preserved across stop(), so a session summary survives the reset.
        self._final_statistics: PlaybackStatistics | None = None
        # Highest Merge Memory version applied. Refreshes at or below this are
        # rejected, so an OCR frame delayed in flight cannot overwrite a newer one.
        self._source_version = 0

        # Serializes state changes so a pause arriving mid-sentence cannot
        # interleave with the advance that follows it.
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def provider(self):
        """Active speech provider."""
        return self._provider

    @property
    def sink(self) -> AudioSink:
        """Active audio sink."""
        return self._sink

    @property
    def ambient_provider(self) -> AmbientProviderInterface | None:
        """Active ambient audio provider."""
        return self._ambient

    @property
    def scene_controller(self) -> SceneController | None:
        """Active scene controller."""
        return self._scene

    @property
    def audio_generation(self) -> int:
        """Active concurrency version token."""
        return self._audio_generation

    def audio_configuration(self) -> dict[str, Any]:
        """Public diagnostic snapshot of active audio wiring."""
        return {
            "provider": self.provider_name,
            "sink": type(self._sink).__name__,
            "ambient": type(self._ambient).__name__ if self._ambient else None,
            "scene": type(self._scene).__name__ if self._scene else None,
            "generation": self._audio_generation,
        }

    def get_runtime_state(self) -> AudioRuntimeState:
        """Observable runtime snapshot of both audio layers."""
        ambient_st = AmbientState.STOPPED
        asset = None
        ch = None
        vol = 0.25
        expected = bool(self._scene is not None)
        mech = "SOUND_OBJECT"

        if self._ambient:
            if not getattr(self._ambient, "is_enabled", False):
                ambient_st = AmbientState.DISABLED
            elif getattr(self._ambient, "is_paused", False) or (self._machine.state is PlaybackState.PAUSED and self._ambient_was_active):
                ambient_st = AmbientState.PAUSED
            elif getattr(self._ambient, "is_playing", False):
                ambient_st = AmbientState.PLAYING
            else:
                ambient_st = AmbientState.STOPPED

            asset = getattr(self._ambient, "current_tag", None)
            ch = getattr(self._ambient, "current_channel_id", None)

        return AudioRuntimeState(
            tts_state=self._machine.state,
            tts_voice=self._voice_id,
            tts_rate=self._profile.rate if self._profile else 1.0,
            ambient_state=ambient_st,
            ambient_asset=asset,
            ambient_channel=ch,
            ambient_volume=vol,
            ambient_expected_to_play=expected,
            output_device="Windows Multimedia Mixer",
            playback_mechanism=mech,
            audio_generation=self._audio_generation,
            started_at=self._stats.started_at,
            last_error=self._error,
        )

    def _log(self, event: str, **fields) -> None:
        """Emit one structured line through the app's logging config.

        Sessions interleave in a shared log, so every line carries its id.
        """

        detail = " ".join(f"{k}={v!r}" for k, v in fields.items())
        logger.info("[audio:%s] %s%s", self._session_id, event, f" {detail}" if detail else "")

    # ---------- lifecycle ----------

    async def start(
        self,
        *,
        pointer: ReadingPointer,
        text: str,
        profile: AudioProfile | None = None,
        voice_id: str | None = None,
    ) -> PlaybackState:
        """Begin playback from `pointer`.

        `text` is a clean paragraph from Merge Memory, not raw OCR.
        """

        # Retire any previous loop before rebuilding state, so an old task
        # cannot keep speaking from the queue we are about to replace.
        await self._cancel_loop()

        async with self._lock:
            self._profile = profile or self._profile
            self._voice_id = voice_id or self._voice_id
            self._error = None
            self._pending_seek = None
            self._current = None

            self._pointer.update(pointer)

            # Sentences are numbered from the start of the paragraph, then the
            # ones already behind the pointer are dropped. Numbering from the
            # pointer instead would label the paragraph's first sentence with
            # the pointer's index and disagree with seek().
            chunks = segment_sentences(
                text,
                start_pointer=pointer.at_paragraph_start(),
                rate=self._profile.rate,
            )
            self._queue.replace(chunks)
            self._queue.drop_before(pointer)

            # Statistics belong to the reading *session*, not to one start()
            # call. A page turn calls start() again to rebuild the queue for new
            # text — that is mid-session, so the tally must carry over. Only a
            # start() from IDLE (the state stop() leaves behind) begins a new
            # session and resets. Getting this wrong silently zeroes a reader's
            # analytics at every page turn.
            if self._machine.state is PlaybackState.IDLE:
                self._stats.reset()
                self._stats.started_at = self._clock()
                self._final_statistics = None
            else:
                # Mid-session restart (page turn): bank the reading time before
                # the IDLE/READY transitions below zero the machine's clock.
                self._stats.carried_reading_ms += self._machine.elapsed_reading_ms

            self._stats.pages.add(pointer.page_index)
            # Merge versions are per-page, so a new page starts from zero again.
            self._source_version = 0

            if self._machine.state is not PlaybackState.IDLE:
                self._machine.transition_to(PlaybackState.IDLE)
            self._machine.transition_to(PlaybackState.READY)

            if not self._queue:
                self._machine.transition_to(PlaybackState.FINISHED)
                self._log("playback_finished", reason="no_sentences")
                return self._machine.state

            self._machine.transition_to(PlaybackState.PLAYING)
            self._log(
                "playback_started",
                pointer=pointer.sentence_order_key(),
                sentences=self._queue.size(),
                profile=self._profile.name,
                provider=self.provider_name,
            )

        self._audio_generation += 1
        current_gen = self._audio_generation
        self._spawn_loop()
        
        # Fire and forget scene evaluation + crossfade with generation token
        if self._scene and self._ambient:
            asyncio.create_task(self._ambient_fire_and_forget(pointer, text, current_gen))

        return self._machine.state

    async def _ambient_fire_and_forget(self, pointer: ReadingPointer, text: str, generation: int) -> None:
        """Evaluates scene and crossfades ambient track without blocking TTS."""
        if not self._scene or not self._ambient:
            return
        
        try:
            decision = await self._scene.evaluate(pointer=pointer, paragraph=text)
            if generation != self._audio_generation:
                logger.info(
                    "[audio:%s] Discarding stale ambient evaluation (gen %d != %d)",
                    self._session_id,
                    generation,
                    self._audio_generation,
                )
                return
            await self._ambient.crossfade(decision)
        except Exception as e:
            logger.error("Ambient scene evaluation failed: %s", e)

    async def pause(self, *, reason: PauseReason = PauseReason.USER) -> PlaybackState:
        """Suspend playback, preserving the pointer.

        Meaning Mode cuts the current sentence off rather than waiting for it.
        """

        self._audio_generation += 1
        async with self._lock:
            if self._machine.state not in (
                PlaybackState.PLAYING,
                PlaybackState.WAITING_FOR_POINTER,
            ):
                return self._machine.state

            self._tts_was_active = True
            self._ambient_was_active = bool(self._ambient and getattr(self._ambient, "is_playing", True))

            # The in-flight sentence was cut off, so put it back. Without this,
            # resume() would dequeue the *next* sentence and the reader would
            # lose whatever they only half-heard.
            if self._current is not None:
                self._queue.push_front(self._current)

            self._machine.transition_to(PlaybackState.PAUSED, pause_reason=reason)
            self._stats.pauses += 1
            if reason is PauseReason.MEANING_MODE:
                self._stats.meaning_mode += 1

            self._log(
                "paused",
                reason=reason.value,
                requeued=self._current.text[:40] if self._current else None,
            )

        await self._sink.stop()
        if self._ambient:
            await self._ambient.pause()
        return self._machine.state

    async def resume(self) -> PlaybackState:
        """Continue from the preserved pointer."""

        self._audio_generation += 1
        # Retire the paused loop before starting a new one. It may still be
        # unwinding from the sentence it was cut off in, and _spawn_loop()
        # declines to start a fresh loop while a task is alive — so without this
        # a resume can silently do nothing, or let the stale loop advance the
        # pointer past the sentence pause() just requeued. Cancelling mid-speak
        # loses nothing: pause() already put that sentence back at the head.
        await self._cancel_loop()

        async with self._lock:
            if self._machine.state is not PlaybackState.PAUSED:
                return self._machine.state
            if not self._queue and self._current is None:
                self._machine.transition_to(PlaybackState.FINISHED)
                self._log("playback_finished", reason="nothing_left_to_speak")
                return self._machine.state
            self._machine.transition_to(PlaybackState.PLAYING)
            head = self._queue.peek()
            self._log("resumed", next_sentence=head.text[:40] if head else None)

        self._spawn_loop()
        if self._ambient and self._ambient_was_active:
            await self._ambient.resume()
        return self._machine.state

    async def stop(self) -> PlaybackState:
        """End playback and clear all state."""

        self._audio_generation += 1
        self._tts_was_active = False
        self._ambient_was_active = False

        async with self._lock:
            # Snapshot before IDLE resets the machine's clock, so the summary the
            # AI Engine receives reflects the session that just ended.
            final = self._snapshot_statistics()

            self._machine.transition_to(PlaybackState.IDLE)
            self._queue.clear()
            self._pointer.reset()
            self._current = None
            self._pending_seek = None
            self._final_statistics = final

            self._log(
                "session_ended",
                sentences=final.sentences_spoken,
                words=final.words_spoken,
                pages=final.pages_read,
                reading_ms=final.reading_time_ms,
                wpm=round(final.average_wpm, 1),
            )

        await self._sink.stop()
        await self._cancel_loop()
        if self._ambient:
            await self._ambient.stop()
        return self._machine.state

    # ---------- pointer changes ----------

    async def seek(
        self, *, pointer: ReadingPointer, text: str | None = None
    ) -> PlaybackState:
        """Jump to `pointer` after the current sentence finishes.

        While idle or paused the jump applies at once, since nothing is in
        flight to protect.

        A seek that arrives after the page ran out of sentences restarts
        playback. FINISHED means "nothing left to speak", not "this page is
        over": the reader is still on it, and a gesture pointing at a later
        paragraph hands over text that was never queued. Without the restart the
        queue fills and no loop is alive to drain it, so narration stops for the
        rest of the page and only a page turn brings it back. A one-fragment
        paragraph — routine on a real OCR page, where the reader's hand cuts a
        line down to a word — is enough to trigger it.
        """

        async with self._lock:
            if text is not None:
                # Fresh text: rebuild the queue from the paragraph, then drop
                # everything the reader has already moved past.
                chunks = segment_sentences(
                    text,
                    start_pointer=pointer.at_paragraph_start(),
                    rate=self._profile.rate,
                )
                self._queue.replace(chunks)
                skipped = self._queue.drop_before(pointer)
            else:
                # No new text: keep what is queued, minus what is now behind us.
                # Otherwise the next dequeue would pull a stale earlier sentence
                # and quietly undo the jump.
                skipped = self._queue.drop_before(pointer)

            self._stats.reading_updates += 1
            self._stats.pages.add(pointer.page_index)

            deferred = self._machine.state is PlaybackState.PLAYING
            self._log(
                "pointer_changed",
                to=pointer.sentence_order_key(),
                skipped=skipped,
                had_text=text is not None,
                deferred=deferred,
                queue_version=self._queue.version,
            )

            if deferred:
                # Defer the jump; the loop applies it once the sentence finishes.
                self._pending_seek = pointer
                self._machine.transition_to(PlaybackState.WAITING_FOR_POINTER)
                return self._machine.state

            self._pointer.update(pointer)

            # Nothing was in flight. If the page had run dry and this seek
            # brought sentences with it, the loop has already returned and has to
            # be restarted — see the note in the docstring.
            revive = self._revive_if_dry(pointer)

        if revive:
            self._spawn_loop()
        return self._machine.state

    async def refresh_queue(
        self,
        *,
        text: str | None = None,
        sentences: list[SentenceChunk] | None = None,
        source_version: int | None = None,
    ) -> bool:
        """Reload pending sentences after a Merge Memory update.

        `text` is the whole current paragraph as Merge Memory now has it. A
        sentence being spoken is already dequeued, so it is never disturbed — only
        what comes after it is rewritten. With nothing being spoken the anchor is
        merely where the reader is, and that sentence is rewritten too, because it
        has not been heard yet.

        `source_version` is Merge Memory's own version for this text. Supply it
        and out-of-order refreshes are rejected: OCR frames travel over HTTP and
        can arrive reordered, and applying an older frame after a newer one would
        regress the text the reader is about to hear. Omit it and every refresh
        is applied, which is fine for a single in-process caller.

        Returns True when the refresh was applied.
        """

        async with self._lock:
            if sentences is None and text is None:
                return False

            if source_version is not None and source_version <= self._source_version:
                self._stats.stale_rejected += 1
                self._log(
                    "queue_refresh_rejected",
                    reason="stale_source_version",
                    received=source_version,
                    current=self._source_version,
                )
                return False

            anchor = (
                self._current.pointer if self._current else self._pointer.current_pointer()
            )

            if sentences is None:
                sentences = segment_sentences(
                    text,
                    start_pointer=anchor.at_paragraph_start(),
                    rate=self._profile.rate,
                )

            # A sentence is in flight only while one is actually being spoken.
            # `_current` alone is not that test: `pause()` pushes the interrupted
            # sentence back to the head of the queue and leaves `_current` set, so
            # a refresh arriving during a Meaning Mode hold would read a *queued*
            # sentence as in flight.
            in_flight = self._current is not None and self._machine.state in (
                PlaybackState.PLAYING,
                PlaybackState.WAITING_FOR_POINTER,
            )

            before = self._queue.version
            if in_flight:
                # Already dequeued and being spoken, so the rewrite starts after
                # it and never restarts what the reader is hearing.
                self._queue.replace_after(anchor, sentences)
            else:
                # Nothing in flight: the anchor is where the reader *is*, and that
                # sentence has not been spoken. `replace_after` is exclusive, so
                # using it here silently discards it — and a paragraph that
                # segments to a single sentence is discarded entirely, which is
                # most refreshes on a real OCR page. Replace-then-drop is the same
                # inclusive idiom `seek` and `start` already use.
                self._queue.replace(sentences)
                self._queue.drop_before(anchor)
            if source_version is not None:
                self._source_version = source_version

            self._stats.queue_refreshes += 1
            self._log(
                "queue_refreshed",
                anchor=anchor.sentence_order_key(),
                anchor_in_flight=in_flight,
                pending=self._queue.size(),
                queue_version=f"{before} -> {self._queue.version}",
                source_version=source_version,
            )
            revive = self._revive_if_dry(anchor)

        if revive:
            self._spawn_loop()
        return True

    def set_profile(self, profile: AudioProfile) -> None:
        """Swap the delivery profile. Takes effect on the next sentence."""

        self._profile = profile

    def set_voice(self, voice_id: str | None) -> None:
        self._voice_id = voice_id

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "provider_name", "unknown")

    async def list_voices(self) -> list[Voice]:
        """Voices offered by the active provider."""

        return await self._provider.get_available_voices()

    async def wait_for_idle(self, *, timeout: float | None = 5.0) -> None:
        """Await the playback loop settling.

        Playback runs as a background task, so callers and tests need a way to
        wait for it rather than polling.
        """

        task = self._task
        if task is None or task.done():
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    # ---------- playback loop ----------

    def _revive_if_dry(self, pointer: ReadingPointer) -> bool:
        """Bring the machine back to PLAYING if it ran dry and has work again.

        Returns whether the caller must `_spawn_loop()` — the transition happens
        under the caller's lock, the spawn deliberately outside it.

        Shared by the seek path and `refresh_queue` because both can hand a
        finished engine new sentences, and only the seek path used to notice. A
        refresh that arrives after the queue ran dry leaves its sentences queued
        with no loop to speak them: the reader hears the paragraph stop partway
        because OCR was still improving it, which is not the reader's doing and
        so is not theirs to undo.

        PAUSED is left alone. The reader stopped on purpose and `resume()` is
        what undoes that — reviving here would restart narration under someone
        who put the book down.
        """

        if self._machine.state is not PlaybackState.FINISHED or not self._queue:
            return False

        # READY zeroes the machine's clock, so bank the page's reading time
        # first — the same reason `start()` carries it on a page turn.
        self._stats.carried_reading_ms += self._machine.elapsed_reading_ms
        self._machine.transition_to(PlaybackState.READY)
        self._machine.transition_to(PlaybackState.PLAYING)
        self._log(
            "playback_revived",
            pointer=pointer.sentence_order_key(),
            sentences=self._queue.size(),
        )
        return True

    def _spawn_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _cancel_loop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        """Speak queued sentences until interrupted or exhausted."""

        try:
            while True:
                async with self._lock:
                    if self._machine.state not in (
                        PlaybackState.PLAYING,
                        PlaybackState.WAITING_FOR_POINTER,
                    ):
                        return

                    chunk = self._queue.dequeue()
                    if chunk is None:
                        self._current = None
                        self._machine.transition_to(PlaybackState.FINISHED)
                        return

                    self._current = chunk
                    self._pointer.update(chunk.pointer)
                    profile = self._profile
                    voice_id = self._voice_id

                await self._speak(chunk, profile, voice_id)

                async with self._lock:
                    if self._machine.state is PlaybackState.PAUSED:
                        # Paused mid-sentence; resume() picks up from here. Not
                        # counted: pause() requeued it and it will be spoken again.
                        return

                    # Finished uninterrupted, so it counts exactly once.
                    self._count_spoken(chunk)

                    if (
                        self._machine.state is PlaybackState.WAITING_FOR_POINTER
                        and self._pending_seek is not None
                    ):
                        self._pointer.update(self._pending_seek)
                        self._pending_seek = None
                        self._machine.transition_to(PlaybackState.PLAYING)
                    elif self._machine.state is PlaybackState.PLAYING:
                        self._pointer.advance()

                    self._current = None

                    if self._machine.state is not PlaybackState.PLAYING:
                        return
                    if not self._auto_advance:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — keep a crash out of the caller's task
            logger.exception("Playback loop failed")
            self._error = str(e)

    async def _speak(
        self, chunk: SentenceChunk, profile: AudioProfile, voice_id: str | None
    ) -> None:
        """Synthesize and play one sentence, then honour the profile's pause."""

        response = await self._provider.synthesize(
            SpeechRequest(text=chunk.text, profile=profile, voice_id=voice_id)
        )

        if not response.ok:
            self._error = response.error
            logger.warning("Synthesis failed for %r: %s", chunk.text[:40], response.error)
            return

        # Providers that speak directly to the device return no bytes.
        if response.audio:
            await self._sink.play(response.audio, content_type=response.content_type)

        if profile.pause_after_sentence_ms:
            await asyncio.sleep(profile.pause_after_sentence_ms / 1000)

    # ---------- statistics ----------

    def _count_spoken(self, chunk: SentenceChunk) -> None:
        """Tally one fully spoken sentence, and note a page change."""

        self._stats.sentences += 1
        self._stats.words += len(chunk.text.split())
        self._stats.characters += len(chunk.text)

        page = chunk.pointer.page_index
        if page not in self._stats.pages:
            self._stats.pages.add(page)
            self._log("page_changed", page=page)

    def _snapshot_statistics(self) -> PlaybackStatistics:
        """Build the analytics snapshot from the counters and both clocks."""

        reading_ms = self._stats.carried_reading_ms + self._machine.elapsed_reading_ms
        playback_ms = (
            int((self._clock() - self._stats.started_at) * 1000)
            if self._stats.started_at is not None
            else 0
        )

        minutes = reading_ms / 60_000
        wpm = self._stats.words / minutes if minutes > 0 else 0.0

        return PlaybackStatistics(
            sentences_spoken=self._stats.sentences,
            words_spoken=self._stats.words,
            characters_spoken=self._stats.characters,
            pages_read=len(self._stats.pages),
            pause_count=self._stats.pauses,
            meaning_mode_count=self._stats.meaning_mode,
            reading_updates=self._stats.reading_updates,
            queue_refreshes=self._stats.queue_refreshes,
            stale_updates_rejected=self._stats.stale_rejected,
            playback_time_ms=playback_ms,
            reading_time_ms=reading_ms,
            average_wpm=round(wpm, 2),
        )

    @property
    def final_statistics(self) -> PlaybackStatistics | None:
        """Statistics captured at the last stop(), for session summaries.

        `stop()` resets the machine's clock, so the AI Engine needs the snapshot
        taken just before that rather than the live counters.
        """

        return self._final_statistics

    # ---------- status ----------

    def get_status(self) -> PlaybackStatus:
        return PlaybackStatus(
            state=self._machine.state,
            session_id=self._session_id,
            pointer=self._pointer.current_pointer() if self._pointer.is_set else None,
            current_sentence=self._current.text if self._current else None,
            profile_name=self._profile.name,
            provider=getattr(self._provider, "provider_name", "unknown"),
            voice_id=self._voice_id,
            queued_sentences=self._queue.size(),
            queue_version=self._queue.version,
            pause_reason=self._machine.pause_reason,
            elapsed_reading_ms=self._machine.elapsed_reading_ms,
            statistics=self._snapshot_statistics(),
            error=self._error,
        )
