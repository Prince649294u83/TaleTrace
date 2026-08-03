"""Reading Speed capability interfaces.

Protocol definitions only — no implementations, no state. Consumers code against
these so the concrete tracker and service can be replaced (by a placeholder, a
fake, or a persistence-backed version) without any caller changing.

These Protocols are documentation, not enforcement: nothing checks them at
runtime, so they only help if they match reality. `tests/test_reading_speed.py`
asserts conformance for exactly that reason — the audio engine's interfaces had
silently drifted from its implementation, and only an executable check caught it.

The shape of this file is itself a contract: there is no `set_pace()`, no
`advance()`, no playback control anywhere below. Reading Speed predicts and
reports; it never drives.
"""

from typing import Protocol

from backend.app.modules.audio_engine.models import PlaybackStatistics, ReadingPointer
from backend.app.modules.reading_speed.models import (
    ContentMap,
    ProgressSnapshot,
    ReaderProfile,
    ReadingBaseline,
    ReadingPrediction,
    SessionAnalytics,
)


class ProgressTrackerInterface(Protocol):
    """Observed progress for one reading session.

    The nine session events are the nine methods here rather than messages on a
    bus. The decoupling that matters is preserved — a tracker never calls Gesture,
    OCR, AI, or the Audio Engine, and cannot tell which of them is reporting — but
    no message infrastructure has to exist for it.

    The pointer held here is a *copy* of the one the Reading Engine owns. Nothing
    on this interface can move the reader.
    """

    def session_started(
        self, *, pointer: ReadingPointer | None = None, content: ContentMap | None = None
    ) -> None:
        """SESSION_STARTED. Reset every counter and start both clocks."""
        ...

    def session_paused(self) -> None:
        """SESSION_PAUSED. Stop the reading clock; the wall clock runs on."""
        ...

    def session_resumed(self) -> None:
        """SESSION_RESUMED. Restart the reading clock."""
        ...

    def meaning_mode_on(self) -> None:
        """MEANING_MODE_ON. A pause that is also evidence about the page.

        Counted apart from a plain pause: a user pause says nothing about the
        text, while Meaning Mode says the reader hit something they could not read
        past.
        """
        ...

    def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. Resume reading."""
        ...

    def lookup_completed(self) -> None:
        """LOOKUP_COMPLETED. The reader asked what a word meant and got an answer."""
        ...

    def pointer_updated(self, pointer: ReadingPointer, *, corrected: bool = False) -> None:
        """READING_POINTER_UPDATED. Copy a new position from the pointer's owner.

        `corrected=True` marks a gesture correction, which is friction evidence.
        Ordinary forward movement is not.
        """
        ...

    def page_changed(self, pointer: ReadingPointer) -> None:
        """PAGE_CHANGED. Close the page being left and open the next."""
        ...

    def session_finished(self) -> None:
        """SESSION_FINISHED. Stop both clocks and close the final page.

        Idempotent: the audio engine finishing and the reader closing the book both
        legitimately call this, and neither knows about the other.
        """
        ...

    def snapshot(self) -> ProgressSnapshot:
        """Current progress. Computed on read, so never stale."""
        ...

    def set_content(self, content: ContentMap) -> bool:
        """Adopt a Merge Memory map. False when rejected as stale.

        Merge Memory stays the source of truth; this only reads from it.
        """
        ...


class ReadingSpeedServiceInterface(Protocol):
    """Per-session prediction and end-of-session analysis.

    One tracker per `session_id`, mirroring `AudioSessionManager`. A shared
    tracker means one reader's page turn closes another reader's page — a defect
    no single-reader test can expose, which is how the audio engine came to have
    the same bug.
    """

    def baseline_for(self, reader_id: str) -> ReadingBaseline:
        """This reader's baseline, defaulting rather than failing.

        A reader with no baseline still gets predictions; the result is marked
        DEFAULT so consumers can see the number is an assumption.
        """
        ...

    def profile_for(self, reader_id: str) -> ReaderProfile:
        """Identity plus baseline."""
        ...

    def calibrate(self, reader_id: str, *, word_count: int, elapsed_ms: int) -> ReadingBaseline:
        """Measure and store a baseline from a timed passage.

        Raises `CalibrationError` on implausible input rather than clamping.
        """
        ...

    def set_manual_baseline(self, reader_id: str, *, baseline_wpm: float) -> ReadingBaseline:
        """Record a reader-supplied pace."""
        ...

    def apply_suggested_baseline(self, summary: SessionAnalytics) -> ReadingBaseline:
        """Move a baseline using a finished session, or leave it unchanged.

        Deliberately separate from `finish_session`: reading a summary must not
        change the reader's profile as a side effect.
        """
        ...

    def start_session(
        self,
        *,
        session_id: str,
        reader_id: str,
        pointer: ReadingPointer | None = None,
        content: ContentMap | None = None,
    ) -> ProgressSnapshot:
        """SESSION_STARTED for a new session."""
        ...

    def predict(self, session_id: str) -> ReadingPrediction:
        """Where the reader would be at their baseline pace.

        Computed on read. Nothing is cached and no loop runs, so the answer is
        current by construction.
        """
        ...

    def observed_wpm(self, session_id: str) -> float:
        """The reader's pace so far this session, 0.0 when too early to say."""
        ...

    def finish_session(
        self, session_id: str, *, playback: PlaybackStatistics | None = None
    ) -> SessionAnalytics:
        """SESSION_FINISHED. Stop the clocks and summarize.

        Supply `playback` when TTS was on: the audio engine counted words while
        speaking them, which beats anything inferred from pointer movement.
        """
        ...

    def close(self, session_id: str) -> bool:
        """Discard a session's tracker. False if there was none."""
        ...
