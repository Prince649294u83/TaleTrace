"""Analysis after the fact. No live decisions, no writes to anything.

Two jobs:

    difficulty per page   —  was this page hard, or was the reader interrupted?
    a session summary     —  what the whole sitting revealed

Everything here runs at page end or session end. Nothing here is consulted
during reading, and nothing it returns changes playback, pointer, or content.

The central problem this file exists to handle: **time alone cannot identify a
hard page.** A reader who spent four minutes on a page because the vocabulary
was dense and a reader who spent four minutes because they answered the door
produce identical timings. So a slow page is only evidence; the verdict also
needs the lookups, meaning-mode requests, pointer corrections and re-reads that
distinguish struggling from being away. Every verdict carries the evidence that
produced it, so a caller can always see why.

`PageObservation` lives here rather than in `models.py` because it is an input
to analysis, not a shape any other module stores or exchanges.
"""

import logging

from pydantic import BaseModel, ConfigDict, Field

from backend.app.modules.audio_engine.models import PlaybackStatistics
from backend.app.modules.reading_speed import calibration
from backend.app.modules.reading_speed.models import (
    DifficultyLevel,
    DifficultyMetrics,
    ProgressSnapshot,
    ReadingBaseline,
    SessionAnalytics,
)
from backend.app.modules.reading_speed.predictor import expected_ms_for

logger = logging.getLogger(__name__)

# A page has to run this far over its predicted time before slowness counts as
# evidence at all. Below it, the difference is noise: page boundaries, a glance
# at an illustration, a sip of coffee.
SLOW_RATIO = 0.25
VERY_SLOW_RATIO = 0.75

# Fast enough to suggest the page was easy rather than mistimed.
FAST_RATIO = -0.20

# How many friction signals (lookups, meaning requests, corrections, revisits)
# it takes to corroborate a slow page.
FRICTION_FOR_MEDIUM = 1
FRICTION_FOR_HIGH = 3

# A page too short to time reliably. Difficulty stays UNKNOWN below this rather
# than being guessed from a handful of words.
MIN_PAGE_WORDS = 30

# Reading time below this cannot produce a pace worth reporting. Dividing 87 words
# by 15ms yields 348,000 wpm — arithmetically correct and completely meaningless,
# and it propagates: the number reaches the UI, and `calibration.suggest_baseline`
# has to reject it as implausible afterwards rather than never being handed it.
# Session pace is reported as 0.0 below this, matching `predictor.observed_wpm`,
# which already declines to divide by an interval this short.
MIN_MEASURABLE_READING_MS = 1_000


class PageObservation(BaseModel):
    """What actually happened on one page.

    Recorded by the tracker as the reader leaves a page. Deliberately raw: the
    counts are observations, and every conclusion drawn from them is computed
    here so the inference is in one place and can be revised without rewriting
    what was recorded.
    """

    model_config = ConfigDict(frozen=True)

    page_index: int
    words: int = Field(default=0, ge=0)
    reading_ms: int = Field(default=0, ge=0)
    lookups: int = Field(default=0, ge=0)
    meaning_requests: int = Field(default=0, ge=0)
    pointer_corrections: int = Field(default=0, ge=0)
    revisits: int = Field(default=0, ge=0)

    @property
    def friction(self) -> int:
        """Total non-time evidence that the reader was working at this page."""

        return (
            self.lookups
            + self.meaning_requests
            + self.pointer_corrections
            + self.revisits
        )


def assess_page(observation: PageObservation, baseline: ReadingBaseline) -> DifficultyMetrics:
    """Rate one page, and say why.

    Order matters and mirrors how the conclusion is actually justified: measure
    the deviation first, then look for corroborating friction, and only then name
    a difficulty. A slow page with no friction is reported as UNKNOWN rather than
    HIGH — being away from the book is not a reading difficulty, and calling it
    one would quietly poison every downstream average.

    Returns UNKNOWN whenever the inputs cannot support a verdict: an uncalibrated
    baseline (nothing to deviate from), too few words, or no recorded time.
    """

    expected_ms = expected_ms_for(observation.words, baseline)
    deviation_ms = observation.reading_ms - expected_ms
    ratio = round(deviation_ms / expected_ms, 3) if expected_ms > 0 else 0.0

    difficulty, evidence = _classify(observation, baseline, expected_ms, ratio)

    return DifficultyMetrics(
        page_index=observation.page_index,
        words=observation.words,
        expected_ms=expected_ms,
        actual_ms=observation.reading_ms,
        deviation_ms=deviation_ms,
        deviation_ratio=ratio,
        lookups=observation.lookups,
        meaning_requests=observation.meaning_requests,
        pointer_corrections=observation.pointer_corrections,
        revisits=observation.revisits,
        difficulty=difficulty,
        evidence=evidence,
    )


def _classify(
    observation: PageObservation,
    baseline: ReadingBaseline,
    expected_ms: int,
    ratio: float,
) -> tuple[DifficultyLevel, tuple[str, ...]]:
    """Pick a difficulty level and collect the reasons for it."""

    if not baseline.is_evidence:
        return DifficultyLevel.UNKNOWN, ("baseline is not a measurement",)
    if observation.words < MIN_PAGE_WORDS:
        return DifficultyLevel.UNKNOWN, (f"only {observation.words} words on the page",)
    if expected_ms <= 0:
        return DifficultyLevel.UNKNOWN, ("no reading time recorded",)

    # A page crossed faster than this was not read on it — the reader skipped it,
    # or the pointer was dragged past it. Without this the page comes back LOW
    # "within 100% of the predicted time", which reads as a verdict but is really
    # the absence of one: -100% deviation falls through every slow branch and
    # lands on the on-pace default.
    if observation.reading_ms < MIN_MEASURABLE_READING_MS:
        return DifficultyLevel.UNKNOWN, (
            f"only {observation.reading_ms}ms on the page - skipped, not read",
        )

    evidence: list[str] = []
    friction = observation.friction

    if observation.lookups:
        evidence.append(f"{observation.lookups} word lookup(s)")
    if observation.meaning_requests:
        evidence.append(f"{observation.meaning_requests} meaning request(s)")
    if observation.pointer_corrections:
        evidence.append(f"{observation.pointer_corrections} pointer correction(s)")
    if observation.revisits:
        evidence.append(f"{observation.revisits} revisit(s)")

    if ratio >= VERY_SLOW_RATIO:
        evidence.insert(0, f"{ratio:+.0%} slower than the baseline predicts")
        if friction >= FRICTION_FOR_HIGH:
            return DifficultyLevel.HIGH, tuple(evidence)
        if friction >= FRICTION_FOR_MEDIUM:
            return DifficultyLevel.MEDIUM, tuple(evidence)
        evidence.append("no lookups or re-reads — likely an interruption, not difficulty")
        return DifficultyLevel.UNKNOWN, tuple(evidence)

    if ratio >= SLOW_RATIO:
        evidence.insert(0, f"{ratio:+.0%} slower than the baseline predicts")
        if friction >= FRICTION_FOR_MEDIUM:
            return DifficultyLevel.MEDIUM, tuple(evidence)
        evidence.append("mild slowdown with no other signals")
        return DifficultyLevel.LOW, tuple(evidence)

    if ratio <= FAST_RATIO and friction == 0:
        return DifficultyLevel.LOW, (f"{ratio:+.0%} faster than predicted, no lookups",)

    if friction >= FRICTION_FOR_HIGH:
        evidence.insert(0, "on pace, but with repeated lookups")
        return DifficultyLevel.MEDIUM, tuple(evidence)

    evidence.insert(0, f"within {abs(ratio):.0%} of the predicted time")
    return DifficultyLevel.LOW, tuple(evidence)


def summarize_session(
    snapshot: ProgressSnapshot,
    baseline: ReadingBaseline,
    *,
    observations: list[PageObservation],
    playback: PlaybackStatistics | None = None,
) -> SessionAnalytics:
    """Everything one session revealed, computed once at the end.

    Every headline number comes from reading progress — how far the pointer got,
    over the reader's own reading clock — whether or not the session was narrated.
    `playback` is used for exactly one thing: `tts_assisted`, which records that
    narration was on. It is metadata about the session, not an input to the
    measurement, because "words the TTS engine spoke" and "words the reader
    covered" are different quantities and only the second one is reading speed.
    The spoken counts remain available in full as `PlaybackStatistics`.

    `tts_assisted` still matters when reading the number: a session read aloud
    and a session read silently are not comparable measurements of the same
    reader, because TTS paces the reader instead of the reader pacing themselves.
    That is a caveat on the pace, not a reason to compute it differently.

    Returns a suggested baseline but never applies one. Moving a baseline is
    `calibration.adapt()`'s decision, and making it a side effect of asking for a
    summary would mean reading analytics twice changed the reader's profile.
    """

    words_read, reading_ms, wall_ms, pages_read = _totals(snapshot, observations)
    session_wpm = (
        round(words_read / (reading_ms / 60_000.0), 1)
        if reading_ms >= MIN_MEASURABLE_READING_MS and words_read
        else 0.0
    )

    pages = tuple(assess_page(observation, baseline) for observation in observations)
    rated = [page for page in pages if page.difficulty is not DifficultyLevel.UNKNOWN]

    analytics = SessionAnalytics(
        session_id=snapshot.session_id,
        reader_id=snapshot.reader_id,
        baseline_wpm=baseline.baseline_wpm,
        session_wpm=session_wpm,
        words_read=words_read,
        pages_read=pages_read,
        reading_duration_ms=reading_ms,
        wall_duration_ms=wall_ms,
        average_deviation_ratio=(
            round(sum(page.deviation_ratio for page in rated) / len(rated), 3) if rated else 0.0
        ),
        lookup_count=snapshot.lookup_count,
        meaning_requests=snapshot.meaning_mode_count,
        hardest_page=max(rated, key=lambda p: p.deviation_ratio).page_index if rated else None,
        easiest_page=min(rated, key=lambda p: p.deviation_ratio).page_index if rated else None,
        pages=pages,
        tts_assisted=playback is not None,
        baseline_was_evidence=baseline.is_evidence,
        suggested_baseline_wpm=calibration.suggest_baseline(
            baseline, session_wpm=session_wpm, reading_ms=reading_ms, words_read=words_read
        ),
    )

    logger.info(
        "[reading_speed:%s] session summary wpm=%.1f baseline=%.1f words=%d pages=%d "
        "deviation=%+.2f tts=%s",
        snapshot.session_id,
        analytics.session_wpm,
        analytics.baseline_wpm,
        analytics.words_read,
        analytics.pages_read,
        analytics.average_deviation_ratio,
        analytics.tts_assisted,
    )
    return analytics


def _totals(
    snapshot: ProgressSnapshot,
    observations: list[PageObservation],
) -> tuple[int, int, int, int]:
    """The session's headline numbers, from reading progress and nothing else.

    Two sources, and they measure the same thing: the tracker's snapshot is how
    far the reader got, and the per-page observations backstop it — a session
    whose pages were all recorded but whose snapshot was never advanced still
    reports the words it covered instead of zero.

    Narration is deliberately *not* a third source, and it used to be the first.
    `PlaybackStatistics.words_spoken` counts words the TTS engine said out loud,
    which is a fact about the audio engine, not about the reader: narration is
    driven by the pointer and the pointer only advances when a gesture selects a
    word, so a reader who listened to a whole book while pointing four times had
    four words spoken from where they pointed. Preferring that number reported an
    hour-long narrated session as ten words read. It is still reported — as
    `PlaybackStatistics`, next to the sentence count, where it belongs — and
    `tts_assisted` still records that narration was on. It is no longer allowed
    to define reading progress.

    That is also what makes narrated and silent sessions comparable: both read
    `words_confirmed`, which is `content.words_before(pointer)`, and narration
    never moves the pointer. Identical progress gives an identical count by
    construction rather than by coincidence.
    """

    observed_words = sum(observation.words for observation in observations)
    observed_ms = sum(observation.reading_ms for observation in observations)

    words = snapshot.words_confirmed or observed_words
    # The reader's reading clock, both branches, which is what `ProgressSnapshot`
    # documents it as: paused and Meaning Mode intervals excluded. Never
    # playback's `reading_time_ms` — that is time spent speaking, a different
    # quantity, and silently swapping the two is how one field came to mean two
    # things depending on whether narration happened to be on.
    reading_ms = snapshot.elapsed_reading_ms or observed_ms
    wall_ms = snapshot.elapsed_wall_ms
    pages_read = len(observations) or snapshot.pages_visited

    return words, reading_ms, max(wall_ms, reading_ms), pages_read
