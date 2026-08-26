"""Session registry and the module's one public entry point.

Composition only. Every calculation belongs to `predictor`, `analytics`, or
`calibration`; this file binds a tracker to a baseline per session and answers
questions about them. It exists so callers have one object to hold instead of
four, and so `router.py` contains no arithmetic.

One tracker per `session_id`, the same shape as `AudioSessionManager`. A shared
tracker would mean one reader's page turn closing another reader's page — a bug
no single-reader test can see, which is exactly how the audio engine acquired the
same defect before it was fixed.

Baselines are held in memory. Persistence belongs to the database module, and
wiring it in should replace `_baselines` with a repository without touching
anything else here.
"""

import logging
import time
from typing import Callable

from backend.app.modules.audio_engine.models import PlaybackStatistics, ReadingPointer
from backend.app.modules.reading_speed import analytics, calibration, predictor
from backend.app.modules.reading_speed.models import (
    ContentMap,
    DifficultyMetrics,
    ProgressSnapshot,
    ReaderProfile,
    ReadingBaseline,
    ReadingPrediction,
    SessionAnalytics,
)
from backend.app.modules.reading_speed.tracker import ProgressTracker

logger = logging.getLogger(__name__)


class UnknownSessionError(KeyError):
    """A prediction or event was requested for a session that never started."""


class ReadingSpeedService:
    """Per-session progress tracking, prediction, and end-of-session analysis."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._trackers: dict[str, ProgressTracker] = {}
        self._session_readers: dict[str, str] = {}
        self._baselines: dict[str, ReadingBaseline] = {}

    # -------------------------------------------------------------- baseline

    def baseline_for(self, reader_id: str) -> ReadingBaseline:
        """This reader's baseline, defaulting rather than failing.

        A reader with no baseline still gets predictions — refusing would mean a
        first session shows nothing — but the returned baseline is marked DEFAULT
        so every consumer can see it is an assumption.
        """

        existing = self._baselines.get(reader_id)
        if existing is not None:
            return existing
        return calibration.default_baseline(reader_id, clock=time.time)

    def profile_for(self, reader_id: str) -> ReaderProfile:
        return ReaderProfile(reader_id=reader_id, baseline=self.baseline_for(reader_id))

    def restore_baseline(self, baseline: ReadingBaseline) -> None:
        """Adopt a baseline measured in an earlier process.

        Deliberately not `calibrate` or `set_manual_baseline`: both of those
        *decide* a baseline and stamp it with the method that produced it. This
        one is reading back a decision already made and must not relabel it — a
        measured baseline that came back from storage as MANUAL would change
        `is_evidence`, and analytics declines to infer difficulty from a baseline
        that is only a claim.

        Takes the whole `ReadingBaseline` rather than a number for the same
        reason. This is the seam the module docstring describes: when `_baselines`
        becomes a repository, this method is what it replaces.
        """

        self._baselines[baseline.reader_id] = baseline

    def calibrate(
        self, reader_id: str, *, word_count: int, elapsed_ms: int
    ) -> ReadingBaseline:
        """Measure and store a baseline from a timed passage.

        Raises `CalibrationError` on implausible input rather than clamping —
        recording a wrong baseline turns a detectable mistake into a permanent one.
        """

        baseline = calibration.calibrate_from_passage(
            reader_id, word_count=word_count, elapsed_ms=elapsed_ms
        )
        self._baselines[reader_id] = baseline
        return baseline

    def set_manual_baseline(self, reader_id: str, *, baseline_wpm: float) -> ReadingBaseline:
        baseline = calibration.calibrate_manually(reader_id, baseline_wpm=baseline_wpm)
        self._baselines[reader_id] = baseline
        return baseline

    def apply_suggested_baseline(self, summary: SessionAnalytics) -> ReadingBaseline:
        """Move a baseline using a finished session's summary.

        Separate from `finish_session()` on purpose. Reading analytics must not
        change the reader's profile as a side effect, or asking for a summary
        twice would move the baseline twice.
        """

        current = self.baseline_for(summary.reader_id)
        adapted = calibration.adapt(
            current,
            session_wpm=summary.session_wpm,
            reading_ms=summary.reading_duration_ms,
            words_read=summary.words_read,
        )
        self._baselines[summary.reader_id] = adapted
        return adapted

    # -------------------------------------------------------------- sessions

    def start_session(
        self,
        *,
        session_id: str,
        reader_id: str,
        pointer: ReadingPointer | None = None,
        content: ContentMap | None = None,
    ) -> ProgressSnapshot:
        """SESSION_STARTED. Creates the tracker and starts both clocks."""

        tracker = ProgressTracker(
            session_id, reader_id, content=content, clock=self._clock
        )
        tracker.session_started(pointer=pointer, content=content)
        self._trackers[session_id] = tracker
        self._session_readers[session_id] = reader_id
        return tracker.snapshot()

    def tracker(self, session_id: str) -> ProgressTracker:
        """The tracker for `session_id`.

        Raises rather than creating one on demand: a pointer update for an
        unknown session means the caller lost track of the session, and inventing
        a tracker whose clock starts at that moment would silently report a
        plausible-looking pace for a session that never began.
        """

        try:
            return self._trackers[session_id]
        except KeyError:
            raise UnknownSessionError(session_id) from None

    def has(self, session_id: str) -> bool:
        return session_id in self._trackers

    def session_ids(self) -> list[str]:
        return list(self._trackers)

    # ---------------------------------------------------------------- events

    def pause(self, session_id: str) -> ProgressSnapshot:
        tracker = self.tracker(session_id)
        tracker.session_paused()
        return tracker.snapshot()

    def resume(self, session_id: str) -> ProgressSnapshot:
        tracker = self.tracker(session_id)
        tracker.session_resumed()
        return tracker.snapshot()

    def meaning_mode(self, session_id: str, *, active: bool) -> ProgressSnapshot:
        tracker = self.tracker(session_id)
        if active:
            tracker.meaning_mode_on()
        else:
            tracker.meaning_mode_off()
        return tracker.snapshot()

    def lookup_completed(self, session_id: str) -> ProgressSnapshot:
        tracker = self.tracker(session_id)
        tracker.lookup_completed()
        return tracker.snapshot()

    def update_pointer(
        self, session_id: str, pointer: ReadingPointer, *, corrected: bool = False
    ) -> ProgressSnapshot:
        """READING_POINTER_UPDATED / PAGE_CHANGED.

        A page change is inferred from the pointer rather than requiring a
        separate call, so Gesture only ever has to report a position.
        """

        tracker = self.tracker(session_id)
        tracker.pointer_updated(pointer, corrected=corrected)
        return tracker.snapshot()

    def update_content(self, session_id: str, content: ContentMap) -> bool:
        """Adopt a Merge Memory map. False when rejected as stale."""

        return self.tracker(session_id).set_content(content)

    # ------------------------------------------------------------ prediction

    def predict(self, session_id: str) -> ReadingPrediction:
        """Where the reader would be at their baseline pace.

        Computed on read. There is no loop and nothing cached, so the answer is
        current by construction rather than up to a tick stale.
        """

        tracker = self.tracker(session_id)
        reader_id = self._session_readers.get(session_id, tracker.snapshot().reader_id)
        return predictor.predict(
            tracker.snapshot(),
            self.baseline_for(reader_id),
            tracker.content,
            origin_word_offset=tracker.origin_word_offset,
        )

    def observed_wpm(self, session_id: str) -> float:
        tracker = self.tracker(session_id)
        return predictor.observed_wpm(
            tracker.snapshot(),
            tracker.content,
            origin_word_offset=tracker.origin_word_offset,
        )

    def page_difficulties(self, session_id: str) -> list[DifficultyMetrics]:
        """Difficulty for every page the reader has *finished*, in order left.

        Only closed pages appear. The page being read now has no verdict, and
        inventing one from a partial page would rate every page as difficult right
        after the turn — its elapsed time keeps growing while its word count has
        not been credited yet.

        Exists so a live view can show difficulty without importing `analytics`.
        Assessment stays a pure function of already-recorded observations, so
        calling this repeatedly costs nothing and changes nothing.
        """

        tracker = self.tracker(session_id)
        reader_id = self._session_readers.get(session_id, tracker.snapshot().reader_id)
        baseline = self.baseline_for(reader_id)
        return [
            analytics.assess_page(observation, baseline)
            for observation in tracker.observations
        ]

    def finish_session(
        self, session_id: str, *, playback: PlaybackStatistics | None = None
    ) -> SessionAnalytics:
        """SESSION_FINISHED. Stops the clocks and returns the summary.

        Supply `playback` when TTS was on: the audio engine counted spoken words
        while speaking them, which beats anything inferred from pointer movement.

        The tracker is kept afterwards so the summary can be fetched again. Call
        `close()` to discard it.
        """

        tracker = self.tracker(session_id)
        tracker.session_finished()
        snapshot = tracker.snapshot()
        return analytics.summarize_session(
            snapshot,
            self.baseline_for(snapshot.reader_id),
            observations=tracker.observations,
            playback=playback,
        )

    def close(self, session_id: str) -> bool:
        """Discard a session's tracker. False if there was none."""

        self._session_readers.pop(session_id, None)
        return self._trackers.pop(session_id, None) is not None

    def close_all(self) -> int:
        count = len(self._trackers)
        self._trackers.clear()
        self._session_readers.clear()
        return count


# Module-level singleton, matching `audio_engine.session_manager`. Routes share
# it so a prediction requested by one endpoint sees the events reported to
# another. Tests construct their own instance with an injected clock rather than
# reaching for this one.
reading_speed_service = ReadingSpeedService()
