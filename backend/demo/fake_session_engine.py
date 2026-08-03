"""The simulated Reading Engine — the only writer of session state.

This is where the ownership rule becomes executable rather than aspirational. The
Reading Engine owns `ReadingSessionState` and the pointer; the Audio Engine and
Reading Speed are handed copies and can only read them.

Every method below is one event the real system will produce — Gesture reporting a
position, the AI Engine finishing a lookup, OCR merging a frame, the reader turning
a page. Each one does the same three things in the same order:

    1. update the state it owns
    2. tell the Audio Engine        (what to speak)
    3. tell Reading Speed           (what to measure)

Steps 2 and 3 never talk to each other. That is the property worth testing: if
Reading Speed can be deleted from this file and playback still works, the coupling
really is absent. The `_notify_speed` / `_notify_audio` split exists to keep that
visible — a future event that needs them to interact would have to break the shape
of this file, which is the point.

Stands in for the real Reading Engine, which does not exist yet. When it does, only
this file is replaced.
"""

import logging

from backend.app.modules.audio_engine.models import PauseReason, ReadingPointer
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared import ReadingSessionState, SessionEvent
from backend.demo.fake_merge_memory import FakeMergeMemory

logger = logging.getLogger(__name__)


class SimulatedReadingEngine:
    """Owns one reading session. Drives the Audio Engine and Reading Speed.

    `tts` is optional: a session read silently is a real session, and Reading Speed
    has to work without an Audio Engine present. Passing None is not a degraded mode
    — it is the silent-reading case, and the one where Reading Speed's inference
    matters most, since there are no spoken-word counts to fall back on.
    """

    def __init__(
        self,
        *,
        session_id: str,
        reader_id: str,
        memory: FakeMergeMemory,
        speed: ReadingSpeedService,
        tts: PlaybackEngine | None = None,
        tts_enabled: bool | None = None,
    ) -> None:
        self._memory = memory
        self._speed = speed
        self._tts = tts

        self._pointer = ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0)
        self._state = ReadingSessionState(
            session_id=session_id,
            reader_id=reader_id,
            book_id="lighthouse",
            pointer=self._pointer,
            # Having an Audio Engine and narrating through it are different facts.
            # A session can hold a wired engine that is currently silent, which is
            # what makes `tts on` possible mid-session: an engine cannot be
            # conjured after the fact, so one that was never built means narration
            # is unavailable for the whole sitting rather than merely off.
            tts_enabled=(tts is not None) if tts_enabled is None else tts_enabled,
        )
        self._events: list[tuple[SessionEvent, str]] = []
        self._summary = None

    # ------------------------------------------------------------------ reads

    @property
    def state(self) -> ReadingSessionState:
        """The current session state. A frozen copy — readers cannot write back."""

        return self._state

    @property
    def pointer(self) -> ReadingPointer:
        return self._pointer

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def speed(self) -> ReadingSpeedService:
        """Reading Speed, for asking it questions.

        Exposed read-only so the dashboard can request a prediction without
        reaching through a private attribute. Publishing it does not weaken the
        ownership rule: `ReadingSpeedService` has no method that moves the pointer
        or changes the session, so a caller holding this can only ask, never tell.
        """

        return self._speed

    @property
    def memory(self) -> FakeMergeMemory:
        """Merge Memory, for reading text and counts."""

        return self._memory

    @property
    def content(self):
        """The content map both consumers are working from."""

        return self._speed.tracker(self._state.session_id).content

    @property
    def events(self) -> list[tuple[SessionEvent, str]]:
        """Every event this session has emitted, in order."""

        return list(self._events)

    @property
    def summary(self):
        """The finished session's analytics, or None before finish_session()."""

        return self._summary

    def current_text(self) -> str:
        """Merge Memory's current best text for the paragraph being read."""

        return self._memory.paragraph(
            self._pointer.page_index, self._pointer.paragraph_index
        )

    def audio_status(self):
        """The Audio Engine's status, or None when reading silently."""

        return None if self._tts is None else self._tts.get_status()

    @property
    def _narrating(self) -> PlaybackEngine | None:
        """The Audio Engine, but only while narration is actually on.

        Every playback call goes through this rather than testing `self._tts`
        directly. The two are not the same question: an engine can be wired and
        silent, and driving it in that state would narrate a page the reader asked
        not to have narrated — while still leaving `tts_enabled` reading False.
        """

        return self._tts if self._state.tts_enabled else None

    # ----------------------------------------------------------------- events

    async def start_session(self, *, profile=None, voice_id: str | None = None) -> None:
        """SESSION_STARTED. Camera on, reader opens the book."""

        content = self._memory.content_map()

        self._set(is_reading=True, content_version=content.source_version)

        # Reading Speed first, so its clock starts before any audio latency is
        # counted against the reader. Starting it after `tts.start()` would charge
        # provider warm-up time to the first page.
        self._speed.start_session(
            session_id=self._state.session_id,
            reader_id=self._state.reader_id,
            pointer=self._pointer,
            content=content,
        )

        if self._narrating is not None:
            await self._tts.start(
                pointer=self._pointer,
                text=self.current_text(),
                profile=profile,
                voice_id=voice_id,
            )

        self._record(SessionEvent.SESSION_STARTED, f"page {self._pointer.page_index}")

    async def advance_sentence(self) -> bool:
        """The reader finished a sentence and moved to the next.

        Returns False at the end of the book. Advancing off the end of a paragraph
        rolls into the next paragraph, and off the end of a page turns it — the
        reader does not stop at a paragraph boundary, so neither does this.
        """

        content = self._speed.tracker(self._state.session_id).content
        index = content.index_of(self._pointer)

        if index is None or index + 1 >= content.sentence_count:
            return False

        nxt = content.sentences[index + 1].pointer
        if nxt.page_index != self._pointer.page_index:
            await self.turn_page()
            return True

        await self._move_to(nxt, corrected=False, reason="advance")
        return True

    async def gesture_to(self, pointer: ReadingPointer, *, corrected: bool = False) -> bool:
        """READING_POINTER_UPDATED. Gesture reports where the reader is looking.

        `corrected=True` marks the reader dragging the pointer back because it was
        wrong — friction evidence. Ordinary forward movement is not.

        Returns False when the camera is off. Gesture detection has no other input
        source, so a report arriving then did not come from anywhere; accepting it
        would move the pointer on evidence that does not exist.
        """

        if not self._state.camera_active:
            logger.info(
                "[session:%s] gesture ignored - camera off", self._state.session_id
            )
            return False

        await self._move_to(pointer, corrected=corrected, reason="gesture")
        return True

    async def turn_page(self) -> bool:
        """PAGE_CHANGED. The reader turned to the next physical page."""

        nxt = self._pointer.page_index + 1
        if nxt > self._memory.page_count:
            return False

        self._memory.turn_to(nxt)
        pointer = ReadingPointer(page_index=nxt, paragraph_index=0, sentence_index=0)
        self._pointer = pointer
        self._set(pointer=pointer, content_version=0)

        # Reading Speed infers the page change from the pointer, so it needs no
        # separate page-turn call. Its tracker closes the page being left, which is
        # what produces the per-page difficulty verdict.
        self._speed.update_pointer(self._state.session_id, pointer)

        if self._narrating is not None:
            # start(), not seek(): a new page is new text from sentence zero, and
            # start() rebuilds the queue rather than splicing into the old one.
            await self._tts.start(pointer=pointer, text=self.current_text())

        self._record(SessionEvent.PAGE_CHANGED, f"page {nxt}")
        return True

    async def pause(self) -> None:
        """SESSION_PAUSED. The reader set the book down."""

        if self._state.is_paused:
            return

        self._set(is_paused=True)
        self._speed.pause(self._state.session_id)
        if self._narrating is not None:
            await self._tts.pause(reason=PauseReason.USER)
        self._record(SessionEvent.SESSION_PAUSED, "")

    async def resume(self) -> None:
        """SESSION_RESUMED. The reader picked it back up."""

        if not self._state.is_paused:
            return

        self._set(is_paused=False)
        self._speed.resume(self._state.session_id)
        if self._narrating is not None:
            await self._tts.resume()
        self._record(SessionEvent.SESSION_RESUMED, "")

    async def meaning_mode_on(self) -> None:
        """MEANING_MODE_ON. The reader tapped a word they do not know.

        Both consumers stop, for different reasons: the Audio Engine because a
        reader who does not understand a word should not have to wait for the
        sentence to finish, and Reading Speed because time spent reading a
        definition is not time spent reading the page.
        """

        if self._state.is_meaning_mode:
            return

        self._set(is_meaning_mode=True)
        self._speed.meaning_mode(self._state.session_id, active=True)
        if self._narrating is not None:
            await self._tts.pause(reason=PauseReason.MEANING_MODE)
        self._record(SessionEvent.MEANING_MODE_ON, "word tapped")

    async def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. Explanation delivered; back to the page."""

        if not self._state.is_meaning_mode:
            return

        self._set(is_meaning_mode=False)
        self._speed.meaning_mode(self._state.session_id, active=False)
        if self._narrating is not None:
            await self._tts.resume()
        self._record(SessionEvent.MEANING_MODE_OFF, "")

    def lookup_completed(self, word: str = "") -> None:
        """LOOKUP_COMPLETED. The AI Engine answered a lookup.

        Not async and does not touch the Audio Engine: a completed lookup is a fact
        about the reader, not an instruction to playback. It reaches Reading Speed
        only, as friction evidence for the page.
        """

        self._speed.lookup_completed(self._state.session_id)
        self._record(SessionEvent.LOOKUP_COMPLETED, word or "word explained")

    async def ocr_refine(self) -> tuple[int, bool]:
        """CONTENT_UPDATED. A new OCR frame improved the current page.

        Returns the new version and whether the Audio Engine accepted it. Both
        consumers version-check independently and either may reject — rejection is
        normal, since frames arrive over HTTP and can overtake each other.

        Returns `(current_version, False)` when the camera is off: with nothing
        streaming there is no frame to merge, and inventing one would let the
        simulator refine text it never saw.
        """

        if not self._state.camera_active:
            logger.info(
                "[session:%s] OCR frame ignored - camera off", self._state.session_id
            )
            return self._state.content_version, False

        version = self._memory.refine()
        self._set(content_version=version)

        self._speed.update_content(
            self._state.session_id, self._memory.content_map(source_version=version)
        )

        applied = True
        if self._narrating is not None:
            applied = await self._tts.refresh_queue(
                text=self.current_text(), source_version=version
            )

        self._record(SessionEvent.CONTENT_UPDATED, f"v{version} applied={applied}")
        return version, applied

    async def deliver_stale_frame(self, *, version: int = 1) -> bool:
        """An out-of-order OCR frame, delivered on purpose to prove it is refused.

        Returns True if it was *wrongly* applied, so a caller can assert on it.
        """

        content = self._memory.content_map(source_version=version)
        speed_took_it = self._speed.update_content(self._state.session_id, content)

        audio_took_it = False
        if self._narrating is not None:
            audio_took_it = await self._tts.refresh_queue(
                text="This is stale text that must never be spoken.",
                source_version=version,
            )

        self._record(
            SessionEvent.CONTENT_UPDATED,
            f"stale v{version} refused by speed={not speed_took_it} audio={not audio_took_it}",
        )
        return speed_took_it or audio_took_it

    async def set_tts(self, *, enabled: bool) -> bool:
        """Turn narration on or off mid-session.

        Returns the state actually reached, which is not always the one asked for:
        a session built without an Audio Engine cannot start narrating, because
        there is no provider or sink to narrate through. Reporting the real state
        rather than the requested one keeps the caller from displaying narration
        that is not happening.

        Not recorded as a `SessionEvent`. Narration is the Reading Engine
        reconfiguring a consumer it owns, not an outside event being reported to
        it — unlike the camera, which is hardware that can leave on its own.

        Worth naming: toggling this mid-session makes `tts_assisted` in the final
        analytics a simplification, since part of the sitting was paced by the
        narrator and part by the reader. The real system should probably refuse
        the toggle once reading has started; the simulator allows it because
        watching the pace change is the point.
        """

        if self._tts is None:
            return False
        if enabled == self._state.tts_enabled:
            return enabled

        self._set(tts_enabled=enabled)
        if enabled:
            # start(), not resume(): the pointer has moved on while narration was
            # off, so the queue has to be rebuilt from where the reader is now.
            await self._tts.start(pointer=self._pointer, text=self.current_text())
        else:
            await self._tts.pause(reason=PauseReason.USER)
        return enabled

    async def set_camera(self, *, active: bool) -> bool:
        """CAMERA_ON / CAMERA_OFF. The ESP32 started or stopped streaming.

        The camera is the only source of both gesture and OCR input, so losing it
        silences two modules at once — `gesture_to` and `ocr_refine` both refuse
        while it is off. It deliberately does *not* stop the session: the reader
        keeps reading and the reading clock keeps running, so the predicted
        position pulls away from the last one actually observed. That growing
        deviation is the honest picture of a blind session, and it is what makes
        an outage visible on screen instead of indistinguishable from a very slow
        reader.

        `advance_sentence` and `turn_page` are left working on purpose. In the
        real system both are consequences of detection and would stop too, but
        here they are how the script and the keyboard move the reader at all —
        gating them would halt the simulation rather than blind it.
        """

        if active == self._state.camera_active:
            return active

        self._set(camera_active=active)
        self._record(
            SessionEvent.CAMERA_ON if active else SessionEvent.CAMERA_OFF,
            "" if active else "gesture and OCR are blind",
        )
        return active

    async def finish_session(self):
        """SESSION_FINISHED. Camera off; stop the clocks and summarise.

        The Audio Engine is stopped *before* the summary is taken, because stopping
        is what finalises its statistics — and those statistics are what Reading
        Speed ingests when TTS was on. Summarising first would hand it a tally that
        is still moving.

        Idempotent, and the summary is cached rather than recomputed. Two callers
        legitimately end a session without knowing about each other — the reader
        closing the book and the Audio Engine reaching the end of the text — and
        the second one must get the same numbers as the first, not a summary of a
        session whose clocks have already stopped.
        """

        if self._summary is not None:
            return self._summary

        self._set(is_reading=False, is_finished=True)

        playback = None
        if self._tts is not None:
            await self._tts.stop()
            playback = self._tts.final_statistics

        self._summary = self._speed.finish_session(
            self._state.session_id, playback=playback
        )
        self._record(
            SessionEvent.SESSION_FINISHED,
            f"{self._summary.words_read} words, {self._summary.pages_read} pages",
        )
        return self._summary

    # -------------------------------------------------------------- internals

    async def _move_to(
        self, pointer: ReadingPointer, *, corrected: bool, reason: str
    ) -> None:
        """Publish a new pointer to both consumers.

        The Audio Engine gets `seek`, which lands the jump *after* the sentence in
        flight; Reading Speed gets it immediately. That difference is correct rather
        than a race: playback must not be cut off mid-sentence, while a measurement
        of where the reader is should not lag behind the reader.
        """

        page_changed = pointer.page_index != self._pointer.page_index
        self._pointer = pointer
        self._set(pointer=pointer)

        self._speed.update_pointer(
            self._state.session_id, pointer, corrected=corrected
        )

        if self._narrating is not None:
            if page_changed:
                await self._tts.start(pointer=pointer, text=self.current_text())
            else:
                await self._tts.seek(pointer=pointer, text=self.current_text())

        if reason == "gesture":
            self._record(
                SessionEvent.READING_POINTER_UPDATED,
                f"sentence {pointer.sentence_index}"
                + (" (correction)" if corrected else ""),
            )

    def _set(self, **changes) -> None:
        """Publish a new state. The only place session state is written."""

        self._state = self._state.model_copy(update=changes)

    def _record(self, event: SessionEvent, detail: str) -> None:
        self._events.append((event, detail))
        logger.info(
            "[session:%s] %s%s",
            self._state.session_id,
            event.value,
            f" {detail}" if detail else "",
        )
