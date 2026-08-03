"""Static Reading Speed placeholders with no tracking or prediction.

Mirrors the pattern used by the gesture, AI, and audio engines: a capability
shell that answers with a stable, identifiable response so consumers can wire
against the boundary before the real service is available.

Every prediction here reports `confidence=0.0` and every analytic reports zeroes.
That is deliberate — a placeholder that returned plausible-looking numbers would
be indistinguishable from a working service, and a caller wiring against it would
believe it had predictions when it had none.
"""

from backend.app.modules.audio_engine.models import PlaybackStatistics, ReadingPointer
from backend.app.modules.reading_speed.models import (
    DEFAULT_BASELINE_WPM,
    CalibrationMethod,
    ContentMap,
    ProgressSnapshot,
    ReaderProfile,
    ReadingBaseline,
    ReadingMode,
    ReadingPrediction,
    SessionAnalytics,
)


class ProgressTrackerPlaceholder:
    """Accepts every event and records nothing."""

    capability = "progress_tracker"

    def __init__(self, session_id: str = "placeholder", reader_id: str = "placeholder") -> None:
        self._session_id = session_id
        self._reader_id = reader_id

    def session_started(
        self, *, pointer: ReadingPointer | None = None, content: ContentMap | None = None
    ) -> None:
        return None

    def session_paused(self) -> None:
        return None

    def session_resumed(self) -> None:
        return None

    def meaning_mode_on(self) -> None:
        return None

    def meaning_mode_off(self) -> None:
        return None

    def lookup_completed(self) -> None:
        return None

    def pointer_updated(self, pointer: ReadingPointer, *, corrected: bool = False) -> None:
        return None

    def page_changed(self, pointer: ReadingPointer) -> None:
        return None

    def session_finished(self) -> None:
        return None

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(
            session_id=self._session_id,
            reader_id=self._reader_id,
            mode=ReadingMode.FINISHED,
        )

    def set_content(self, content: ContentMap) -> bool:
        return False


class ReadingSpeedServicePlaceholder:
    """Answers every query with an explicitly empty, zero-confidence result."""

    capability = "reading_speed_service"

    def baseline_for(self, reader_id: str) -> ReadingBaseline:
        return ReadingBaseline(
            reader_id=reader_id,
            baseline_wpm=DEFAULT_BASELINE_WPM,
            calibrated=False,
            method=CalibrationMethod.DEFAULT,
        )

    def profile_for(self, reader_id: str) -> ReaderProfile:
        return ReaderProfile(
            reader_id=reader_id,
            display_name=self.capability,
            baseline=self.baseline_for(reader_id),
        )

    def calibrate(self, reader_id: str, *, word_count: int, elapsed_ms: int) -> ReadingBaseline:
        return self.baseline_for(reader_id)

    def set_manual_baseline(self, reader_id: str, *, baseline_wpm: float) -> ReadingBaseline:
        return self.baseline_for(reader_id)

    def apply_suggested_baseline(self, summary: SessionAnalytics) -> ReadingBaseline:
        return self.baseline_for(summary.reader_id)

    def start_session(
        self,
        *,
        session_id: str,
        reader_id: str,
        pointer: ReadingPointer | None = None,
        content: ContentMap | None = None,
    ) -> ProgressSnapshot:
        return ProgressSnapshot(
            session_id=session_id, reader_id=reader_id, mode=ReadingMode.FINISHED
        )

    def predict(self, session_id: str) -> ReadingPrediction:
        return ReadingPrediction(session_id=session_id, confidence=0.0)

    def observed_wpm(self, session_id: str) -> float:
        return 0.0

    def finish_session(
        self, session_id: str, *, playback: PlaybackStatistics | None = None
    ) -> SessionAnalytics:
        return SessionAnalytics(session_id=session_id, reader_id=self.capability)

    def close(self, session_id: str) -> bool:
        return False
