"""Reading Speed module boundary.

A complementary prediction service. It reads a reader's established pace and
their observed progress, and reports where the two diverge.

What it does not do, by design: own the reading session, own the reading pointer,
or control TTS. Nothing exported here can move a reader, change playback, or
modify content. The Audio Engine never asks "how fast is this reader?"; Merge
Memory stays the source of truth and is only ever read.
"""

from backend.app.modules.reading_speed.analytics import (
    PageObservation,
    assess_page,
    summarize_session,
)
from backend.app.modules.reading_speed.calibration import (
    CalibrationError,
    adapt,
    calibrate_from_passage,
    calibrate_manually,
    can_adapt,
    default_baseline,
    suggest_baseline,
)
from backend.app.modules.reading_speed.interfaces import (
    ProgressTrackerInterface,
    ReadingSpeedServiceInterface,
)
from backend.app.modules.reading_speed.models import (
    DEFAULT_BASELINE_WPM,
    MAX_PLAUSIBLE_WPM,
    MIN_PLAUSIBLE_WPM,
    CalibrationMethod,
    ContentMap,
    DifficultyLevel,
    DifficultyMetrics,
    ProgressSnapshot,
    ReaderProfile,
    ReadingBaseline,
    ReadingMode,
    ReadingPrediction,
    SentenceSpan,
    SessionAnalytics,
)
from backend.app.modules.reading_speed.predictor import (
    expected_ms_for,
    expected_words_in,
    observed_wpm,
    predict,
    words_remaining_on_page,
)
from backend.app.modules.reading_speed.service import (
    ReadingSpeedService,
    UnknownSessionError,
    reading_speed_service,
)
from backend.app.modules.reading_speed.tracker import ProgressTracker

__all__ = [
    "DEFAULT_BASELINE_WPM",
    "MAX_PLAUSIBLE_WPM",
    "MIN_PLAUSIBLE_WPM",
    "CalibrationError",
    "CalibrationMethod",
    "ContentMap",
    "DifficultyLevel",
    "DifficultyMetrics",
    "PageObservation",
    "ProgressSnapshot",
    "ProgressTracker",
    "ProgressTrackerInterface",
    "ReaderProfile",
    "ReadingBaseline",
    "ReadingMode",
    "ReadingPrediction",
    "ReadingSpeedService",
    "ReadingSpeedServiceInterface",
    "SentenceSpan",
    "SessionAnalytics",
    "UnknownSessionError",
    "adapt",
    "assess_page",
    "calibrate_from_passage",
    "calibrate_manually",
    "can_adapt",
    "default_baseline",
    "expected_ms_for",
    "expected_words_in",
    "observed_wpm",
    "predict",
    "reading_speed_service",
    "suggest_baseline",
    "summarize_session",
    "words_remaining_on_page",
]
