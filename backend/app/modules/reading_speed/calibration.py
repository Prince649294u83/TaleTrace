"""Baseline establishment and recalibration.

Runs once per reader, then goes quiet. Nothing here executes during normal
reading — that is the entire reason it is a separate module from `predictor.py`.

Three ways to get a baseline, in descending order of trustworthiness:

    measure a passage  ->  MEASURED   (evidence)
    ask the reader     ->  MANUAL     (a claim)
    assume             ->  DEFAULT    (a guess)

and one way to move an existing one: `adapt()`, which blends a session's
observed pace into the stored baseline at a deliberately low weight.
"""

import logging
import time
from typing import Callable

from backend.app.modules.reading_speed.models import (
    DEFAULT_BASELINE_WPM,
    MAX_PLAUSIBLE_WPM,
    MIN_PLAUSIBLE_WPM,
    CalibrationMethod,
    ReadingBaseline,
)

logger = logging.getLogger(__name__)

# How much a single session moves the baseline. The 80/20 split is what keeps a
# baseline stable: one distracted session shifts an established pace by a few
# percent rather than redefining it.
ADAPT_WEIGHT_NEW = 0.2
ADAPT_WEIGHT_OLD = 1.0 - ADAPT_WEIGHT_NEW

# Below this, a session is too short to say anything about pace. A reader who
# opens a book, reads two sentences and closes it has produced noise, not a
# measurement.
MIN_ADAPT_READING_MS = 60_000
MIN_ADAPT_WORDS = 150

# A calibration passage shorter than this cannot separate reading speed from
# reaction time.
MIN_CALIBRATION_WORDS = 50
MIN_CALIBRATION_MS = 10_000


class CalibrationError(ValueError):
    """A calibration input that cannot produce a usable baseline."""


def default_baseline(reader_id: str, *, clock: Callable[[], float] = time.time) -> ReadingBaseline:
    """A starting baseline for a reader who has never calibrated.

    Marked DEFAULT and `calibrated=False` so every consumer can tell that the
    number is an assumption. Predictions still work — they must, or a first
    session would show nothing — but `is_evidence` stays False and analytics
    will decline to infer difficulty from it.
    """

    return ReadingBaseline(
        reader_id=reader_id,
        baseline_wpm=DEFAULT_BASELINE_WPM,
        calibrated=False,
        method=CalibrationMethod.DEFAULT,
        sample_count=0,
        updated_at_ms=int(clock() * 1000),
    )


def calibrate_from_passage(
    reader_id: str,
    *,
    word_count: int,
    elapsed_ms: int,
    clock: Callable[[], float] = time.time,
) -> ReadingBaseline:
    """Measure a baseline from a timed passage.

    The reader reads a known passage; we time it. 180 words in 60 seconds is
    180 wpm — the arithmetic is trivial and that is a feature. The value of this
    function is the validation around it, since a stopwatch left running or a
    passage abandoned halfway both produce a number that looks perfectly valid.

    Raises `CalibrationError` rather than clamping. A passage that yields
    1500 wpm was not read, and silently recording 1200 would turn a detectable
    mistake into a permanent wrong baseline.
    """

    if word_count < MIN_CALIBRATION_WORDS:
        raise CalibrationError(
            f"calibration passage needs at least {MIN_CALIBRATION_WORDS} words, got {word_count}"
        )
    if elapsed_ms < MIN_CALIBRATION_MS:
        raise CalibrationError(
            f"calibration needs at least {MIN_CALIBRATION_MS}ms of reading, got {elapsed_ms}"
        )

    wpm = word_count / (elapsed_ms / 60_000.0)
    if not MIN_PLAUSIBLE_WPM <= wpm <= MAX_PLAUSIBLE_WPM:
        raise CalibrationError(
            f"measured {wpm:.0f} wpm from {word_count} words in {elapsed_ms}ms, "
            f"outside the plausible range {MIN_PLAUSIBLE_WPM:.0f}-{MAX_PLAUSIBLE_WPM:.0f}; "
            "the timer was probably left running or the passage was not finished"
        )

    baseline = ReadingBaseline(
        reader_id=reader_id,
        baseline_wpm=round(wpm, 1),
        calibrated=True,
        method=CalibrationMethod.MEASURED,
        sample_count=1,
        updated_at_ms=int(clock() * 1000),
    )
    logger.info(
        "[reading_speed:%s] calibrated wpm=%.1f words=%d elapsed_ms=%d method=measured",
        reader_id,
        baseline.baseline_wpm,
        word_count,
        elapsed_ms,
    )
    return baseline


def calibrate_manually(
    reader_id: str, *, baseline_wpm: float, clock: Callable[[], float] = time.time
) -> ReadingBaseline:
    """Record a reader-supplied pace.

    Kept distinct from MEASURED because a self-reported speed is a claim, and
    readers routinely overestimate. It is still marked `calibrated=True`: the
    reader has made a deliberate statement, which beats our default guess.
    """

    if not MIN_PLAUSIBLE_WPM <= baseline_wpm <= MAX_PLAUSIBLE_WPM:
        raise CalibrationError(
            f"{baseline_wpm:.0f} wpm is outside the plausible range "
            f"{MIN_PLAUSIBLE_WPM:.0f}-{MAX_PLAUSIBLE_WPM:.0f}"
        )

    return ReadingBaseline(
        reader_id=reader_id,
        baseline_wpm=round(baseline_wpm, 1),
        calibrated=True,
        method=CalibrationMethod.MANUAL,
        sample_count=0,
        updated_at_ms=int(clock() * 1000),
    )


def can_adapt(*, reading_ms: int, words_read: int) -> bool:
    """Whether a session carries enough evidence to move the baseline.

    Exposed separately so callers can explain *why* a baseline did not change,
    which is otherwise indistinguishable from the update silently failing.
    """

    return reading_ms >= MIN_ADAPT_READING_MS and words_read >= MIN_ADAPT_WORDS


def adapt(
    baseline: ReadingBaseline,
    *,
    session_wpm: float,
    reading_ms: int,
    words_read: int,
    clock: Callable[[], float] = time.time,
) -> ReadingBaseline:
    """Blend one session's pace into the baseline, or return it unchanged.

    Deliberately *not* called during reading. A baseline that updates live is a
    baseline the reader is being compared against while it moves, which makes
    deviation meaningless — you can no longer tell whether the reader sped up or
    the yardstick shrank. This runs at session end, if at all.

    Returns the original instance untouched when the evidence is too thin or the
    observed pace is implausible, so callers can always assign the result.
    """

    if not can_adapt(reading_ms=reading_ms, words_read=words_read):
        logger.info(
            "[reading_speed:%s] baseline unchanged reason=insufficient_evidence "
            "reading_ms=%d words=%d",
            baseline.reader_id,
            reading_ms,
            words_read,
        )
        return baseline

    if not MIN_PLAUSIBLE_WPM <= session_wpm <= MAX_PLAUSIBLE_WPM:
        logger.warning(
            "[reading_speed:%s] baseline unchanged reason=implausible_session_wpm wpm=%.1f",
            baseline.reader_id,
            session_wpm,
        )
        return baseline

    # An uncalibrated baseline is a guess, so the first real session should
    # replace it outright rather than being averaged with a number we invented.
    if not baseline.calibrated:
        blended = session_wpm
    else:
        blended = ADAPT_WEIGHT_OLD * baseline.baseline_wpm + ADAPT_WEIGHT_NEW * session_wpm

    blended = min(max(blended, MIN_PLAUSIBLE_WPM), MAX_PLAUSIBLE_WPM)

    adapted = ReadingBaseline(
        reader_id=baseline.reader_id,
        baseline_wpm=round(blended, 1),
        calibrated=True,
        method=CalibrationMethod.ADAPTED,
        sample_count=baseline.sample_count + 1,
        updated_at_ms=int(clock() * 1000),
    )
    logger.info(
        "[reading_speed:%s] baseline adapted from=%.1f session=%.1f to=%.1f samples=%d",
        baseline.reader_id,
        baseline.baseline_wpm,
        session_wpm,
        adapted.baseline_wpm,
        adapted.sample_count,
    )
    return adapted


def suggest_baseline(
    baseline: ReadingBaseline, *, session_wpm: float, reading_ms: int, words_read: int
) -> float | None:
    """What `adapt()` would produce, without applying it.

    Lets analytics report a suggested baseline so the decision to accept it can
    belong to the reader or the caller rather than to this module.
    """

    if not can_adapt(reading_ms=reading_ms, words_read=words_read):
        return None
    if not MIN_PLAUSIBLE_WPM <= session_wpm <= MAX_PLAUSIBLE_WPM:
        return None
    if not baseline.calibrated:
        return round(session_wpm, 1)
    return round(ADAPT_WEIGHT_OLD * baseline.baseline_wpm + ADAPT_WEIGHT_NEW * session_wpm, 1)
