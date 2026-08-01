"""Reading Audio Engine capability interfaces.

Protocol definitions only — no implementations, no provider dependencies. The
playback engine and speech providers implement these contracts, and the Reading
Engine codes against `PlaybackEngineInterface` to start/pause/resume playback.

These Protocols are documentation, not enforcement: nothing is checked against
them at runtime, so they only help if they match the implementation. Keep them
in step with `playback_engine.py` and `contracts.md` when any signature moves.
"""

from typing import Protocol

from backend.app.modules.audio_engine.models import (
    AudioProfile,
    PauseReason,
    PlaybackState,
    PlaybackStatistics,
    PlaybackStatus,
    ReadingPointer,
    SentenceChunk,
    SpeechRequest,
    SpeechResponse,
    Voice,
)


class SpeechProviderInterface(Protocol):
    """Provider abstraction for text-to-speech synthesis.

    Every provider (Edge TTS, offline pyttsx3, Azure Speech, ElevenLabs, etc.)
    implements this protocol. The playback engine never knows which provider is
    active; swapping Edge for offline changes nothing in playback logic.
    """

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        """Convert text to speech audio bytes.

        Returns MP3 (Edge) or WAV (offline) bytes. On failure, `response.error`
        is set and `response.audio` is None.
        """
        ...

    async def get_available_voices(self) -> list[Voice]:
        """List voices offered by this provider.

        Edge returns ~400 neural voices; offline returns system-installed voices.
        """
        ...

    @property
    def provider_name(self) -> str:
        """Human-readable provider name for status/logging."""
        ...


class AudioSinkInterface(Protocol):
    """Where synthesized audio goes.

    Separated from the provider so tests can run silently, and so the ESP32
    speaker path can replace local playback later without touching providers.

    `speech_provider.AudioSink` is the structural twin of this Protocol, kept
    there so the module has no import back into `interfaces`.
    """

    async def play(self, audio: bytes, *, content_type: str = "audio/mpeg") -> None:
        """Play audio to completion.

        Must not return until playback finishes: the engine treats the duration
        of this call as the duration of the sentence. A sink that returns early
        makes playback appear instantaneous, and the queue drains before any
        event can interrupt it.
        """
        ...

    async def stop(self) -> None:
        """Interrupt playback immediately. This is what Meaning Mode is."""
        ...


class PlaybackEngineInterface(Protocol):
    """Core playback coordination.

    The Reading Engine owns the reading pointer and calls this interface to
    start, pause, resume, or jump. The engine never queries OCR, Camera, or
    Gesture — it receives pointers and plays the corresponding text.

    One engine per reader. `AudioSessionManager` keys them by `session_id`;
    sharing one engine between readers means one reader's pause stops the
    other's book.
    """

    async def start(
        self,
        *,
        pointer: ReadingPointer,
        text: str,
        profile: AudioProfile | None = None,
        voice_id: str | None = None,
    ) -> PlaybackState:
        """Begin playback from a pointer.

        `text` is the clean paragraph from Merge Memory, not raw OCR. The engine
        segments it into sentences and queues them for playback.

        Also used for page turns. Calling this mid-session rebuilds the queue and
        *keeps* the session statistics; only a call from IDLE starts a new tally.

        Returns READY or PLAYING.
        """
        ...

    async def pause(self, *, reason: PauseReason = PauseReason.USER) -> PlaybackState:
        """Suspend playback (Meaning Mode, user pause).

        The sentence in flight is requeued so resume repeats it rather than
        skipping it, and the reading clock stops.

        Returns PAUSED.
        """
        ...

    async def resume(self) -> PlaybackState:
        """Continue from where paused.

        Returns PLAYING, or FINISHED if nothing is left to speak.
        """
        ...

    async def stop(self) -> PlaybackState:
        """End the session: snapshot statistics, clear the queue, return to IDLE.

        This ends the session. For a page turn call `start()` instead — stopping
        first discards the running tally.

        Returns IDLE.
        """
        ...

    async def seek(
        self, *, pointer: ReadingPointer, text: str | None = None
    ) -> PlaybackState:
        """Jump to a new pointer (Reading Update).

        Finishes the current sentence, then jumps. Never interrupts mid-sentence.
        Omit `text` to keep the queued sentences and drop only what the reader
        has already passed.

        Returns WAITING_FOR_POINTER while playing, otherwise the current state.
        """
        ...

    async def refresh_queue(
        self,
        *,
        text: str | None = None,
        sentences: list[SentenceChunk] | None = None,
        source_version: int | None = None,
    ) -> bool:
        """Rewrite pending sentences after a Merge Memory update.

        The sentence in flight is already dequeued, so it is never disturbed —
        only what follows it is replaced.

        `source_version` is Merge Memory's version for this text. Supply it and
        out-of-order frames are rejected, since OCR updates travel over HTTP and
        an older frame applied after a newer one would regress the text.

        Returns True when the refresh was applied, False when it was rejected as
        stale. Rejection is a normal outcome, not an error.
        """
        ...

    def get_status(self) -> PlaybackStatus:
        """State, pointer, queue version, and live statistics."""
        ...

    @property
    def final_statistics(self) -> PlaybackStatistics | None:
        """The tally captured by the last `stop()`, or None if never stopped.

        Snapshotted before the IDLE transition, which zeroes the reading clock.
        """
        ...

    def set_profile(self, profile: AudioProfile) -> None:
        """Swap the delivery profile. Takes effect on the next sentence."""
        ...

    def set_voice(self, voice_id: str | None) -> None:
        """Swap the voice. Takes effect on the next sentence."""
        ...

    async def list_voices(self) -> list[Voice]:
        """Voices offered by the active provider."""
        ...

    @property
    def provider_name(self) -> str:
        """Name of the active speech provider."""
        ...


class AudioSessionManagerInterface(Protocol):
    """Registry mapping `session_id` to its own playback engine.

    Exists because a single shared engine meant two readers fought over one
    queue and one pointer. Nothing in the route surface revealed that — every
    endpoint behaved correctly with a single reader.
    """

    def get(self, session_id: str) -> PlaybackEngineInterface:
        """Return the engine for `session_id`, creating it on first use."""
        ...

    def has(self, session_id: str) -> bool:
        """Whether an engine exists for `session_id`."""
        ...

    def session_ids(self) -> list[str]:
        """Ids of every live session."""
        ...

    def statuses(self) -> list[PlaybackStatus]:
        """Snapshot every live session, for debugging and isolation checks."""
        ...

    async def close(self, session_id: str) -> bool:
        """Stop a session and discard its engine. False if there was none.

        Stopping first matters: dropping the reference without cancelling the
        playback task leaves it speaking into a sink nobody is listening to.
        """
        ...

    async def close_all(self) -> int:
        """Stop every session. Used on app shutdown and between tests."""
        ...


class PointerManagerInterface(Protocol):
    """Reading pointer ownership and advancement.

    The Reading Engine owns the canonical pointer; the Audio Engine keeps a
    copy and advances it as sentences finish. This interface defines the
    contract for pointer manipulation.
    """

    def current_pointer(self) -> ReadingPointer:
        """The pointer at the start of the currently playing sentence."""
        ...

    def advance(self) -> ReadingPointer:
        """Move to the next sentence in the same paragraph.

        Returns the new pointer.
        """
        ...

    def update(self, pointer: ReadingPointer) -> None:
        """Set the pointer (Reading Update, page turn)."""
        ...

    def reset(self) -> None:
        """Clear the pointer (session end, return to IDLE)."""
        ...


class SentenceQueueInterface(Protocol):
    """Buffer between Merge Memory and playback.

    Sentences are queued ahead of playback so OCR improvements don't restart
    the currently playing sentence. The queue holds 3–5 sentences by default.
    """

    @property
    def version(self) -> int:
        """Monotonic counter, bumped by every mutation.

        `dequeue()` deliberately does not bump it: consuming a sentence is not a
        rewrite of the queue, and a version that changed on every sentence could
        not be used to detect one.
        """
        ...

    def enqueue(self, chunk: SentenceChunk) -> None:
        """Add a sentence to the tail."""
        ...

    def extend(self, chunks: list[SentenceChunk]) -> None:
        """Add several sentences to the tail."""
        ...

    def dequeue(self) -> SentenceChunk | None:
        """Remove and return the head sentence, or None if empty."""
        ...

    def peek(self) -> SentenceChunk | None:
        """Return the head sentence without removing it."""
        ...

    def push_front(self, chunk: SentenceChunk) -> None:
        """Return a sentence to the head.

        Used by `pause()` so the interrupted sentence is spoken again on resume
        instead of being skipped.
        """
        ...

    def clear(self) -> None:
        """Discard all queued sentences (page turn, stop)."""
        ...

    def replace(self, chunks: list[SentenceChunk]) -> None:
        """Replace the whole queue."""
        ...

    def replace_after(self, pointer: ReadingPointer, chunks: list[SentenceChunk]) -> None:
        """Rewrite only the sentences following `pointer`.

        Used on a Merge refresh, so the sentence being spoken survives intact.
        """
        ...

    def drop_before(self, pointer: ReadingPointer) -> int:
        """Discard queued sentences preceding `pointer`. Returns how many.

        Used by a Reading Update that moves the pointer without new text.
        Without this the next dequeue would pull a stale earlier sentence and
        quietly undo the jump.
        """
        ...

    def size(self) -> int:
        """Number of queued sentences."""
        ...
