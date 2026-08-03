"""Prediction. The heart of the module, and the part that changes nothing.

Every function here is pure: same inputs, same output, no stored state, no I/O,
no calls back into any other engine. A prediction is a comparison between where
the reader *is* and where their established pace says they *would be*. It is
never an instruction — nothing in TaleTrace should pause, seek, or re-render
because of anything this file returns.

There is no background loop. A prediction is a pure function of
(baseline, progress, content), so recomputing it on read is cheaper than keeping
a 1 Hz task alive and gives an answer that is current by construction rather
than up to a second stale.

Sign conventions, because the two deviations point opposite ways on purpose:

    deviation_words > 0   reader is further along than predicted  (ahead)
    deviation_ms    > 0   reader took longer than predicted       (behind)

The second matches how a reader describes it: "expected 90 seconds, actually
took 140" is +50, not -50.
"""

import logging

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.reading_speed.models import (
    ContentMap,
    ProgressSnapshot,
    ReadingBaseline,
    ReadingPrediction,
)

logger = logging.getLogger(__name__)

# Reading for less than this says nothing about pace: the first few seconds are
# dominated by finding your place on the page rather than by reading it.
CONFIDENT_AFTER_MS = 30_000

# How much a non-measured baseline discounts confidence. Not zero — a manual or
# default baseline still produces a usable estimate — but low enough that
# analytics can filter on it.
GUESS_CONFIDENCE_FACTOR = 0.4


def expected_words_in(elapsed_ms: int, baseline: ReadingBaseline) -> float:
    """Words a reader at `baseline` would cover in `elapsed_ms` of reading."""

    if elapsed_ms <= 0:
        return 0.0
    return baseline.words_per_ms * elapsed_ms


def expected_ms_for(words: float, baseline: ReadingBaseline) -> int:
    """Milliseconds a reader at `baseline` would spend on `words` words.

    Rounds rather than truncates. `words_per_ms` is a repeating decimal for most
    plausible paces, so truncation drops a millisecond and every deviation built
    on it inherits an off-by-one that looks like a real difference.
    """

    if words <= 0 or baseline.words_per_ms <= 0:
        return 0
    return round(words / baseline.words_per_ms)


def words_remaining_on_page(content: ContentMap, pointer: ReadingPointer) -> int:
    """Words left on `pointer`'s page, counting the current sentence as unread.

    Falls back to `page_word_counts` when the page has no sentence spans, which
    is the normal state for a page OCR has counted but not yet segmented.
    """

    key = pointer.sentence_order_key()
    page = pointer.page_index

    spans = [span for span in content.sentences if span.pointer.page_index == page]
    if not spans:
        return max(content.page_word_counts.get(page, 0), 0)

    return sum(
        span.word_count for span in spans if span.pointer.sentence_order_key() >= key
    )


def _confidence(snapshot: ProgressSnapshot, baseline: ReadingBaseline, content: ContentMap) -> float:
    """How much weight this prediction deserves.

    A prediction is always returned — a UI needs something to display from the
    first second — so unreliability is reported here rather than by withholding
    the result. Three things degrade it: an unmeasured baseline, too little
    reading time, and an empty content map.
    """

    if content.sentence_count == 0 and not content.page_word_counts:
        return 0.0

    time_factor = min(1.0, snapshot.elapsed_reading_ms / CONFIDENT_AFTER_MS)
    evidence_factor = 1.0 if baseline.is_evidence else GUESS_CONFIDENCE_FACTOR
    return round(time_factor * evidence_factor, 3)


def predict(
    snapshot: ProgressSnapshot,
    baseline: ReadingBaseline,
    content: ContentMap,
    *,
    origin_word_offset: int = 0,
) -> ReadingPrediction:
    """Where the reader would be at their baseline pace, versus where they are.

    `origin_word_offset` is where this session started within `content`. It
    matters because a reader who opens the book at page 40 has not read the first
    39 pages, and predicting against word zero would report them permanently and
    absurdly behind.

    Prediction uses `elapsed_reading_ms`, never wall time. Pauses and Meaning
    Mode intervals are excluded, so looking up a word does not make the reader
    look slower — the whole reason the two clocks exist.
    """

    actual_offset = _actual_offset(snapshot, content)
    expected_offset = origin_word_offset + expected_words_in(
        snapshot.elapsed_reading_ms, baseline
    )

    total_words = content.total_words
    expected_pointer = content.pointer_at_word(int(expected_offset))

    words_read = max(actual_offset - origin_word_offset, 0)
    expected_ms = expected_ms_for(words_read, baseline)
    deviation_ms = snapshot.elapsed_reading_ms - expected_ms if words_read else 0

    words_left = max(total_words - actual_offset, 0)
    page_left = words_remaining_on_page(content, snapshot.pointer)

    return ReadingPrediction(
        session_id=snapshot.session_id,
        expected_pointer=expected_pointer,
        expected_word_offset=max(int(expected_offset), 0),
        actual_word_offset=max(actual_offset, 0),
        expected_sentence_index=(
            content.index_of(expected_pointer) if expected_pointer else None
        ),
        actual_sentence_index=content.index_of(snapshot.pointer),
        words_remaining=words_left,
        expected_finish_ms=expected_ms_for(words_left, baseline),
        expected_page_finish_ms=expected_ms_for(page_left, baseline),
        progress_percentage=_percentage(actual_offset, total_words),
        expected_progress_percentage=_percentage(expected_offset, total_words),
        deviation_words=int(actual_offset - expected_offset),
        deviation_ms=deviation_ms,
        deviation_ratio=_ratio(deviation_ms, expected_ms),
        confidence=_confidence(snapshot, baseline, content),
        baseline_wpm=baseline.baseline_wpm,
    )


def observed_wpm(snapshot: ProgressSnapshot, content: ContentMap, *, origin_word_offset: int = 0) -> float:
    """The reader's pace this session so far.

    Computed here rather than stored on `ProgressSnapshot` deliberately: a
    snapshot that carried its own WPM would be a second source of truth
    competing with the baseline, and the two would diverge.

    Returns 0.0 when there is not enough reading time to divide by, which is
    honest — a pace derived from 400ms of reading is a number, not a measurement.
    """

    if snapshot.elapsed_reading_ms <= 0:
        return 0.0
    words = max(_actual_offset(snapshot, content) - origin_word_offset, 0)
    if words <= 0:
        return 0.0
    return round(words / (snapshot.elapsed_reading_ms / 60_000.0), 1)


def _actual_offset(snapshot: ProgressSnapshot, content: ContentMap) -> int:
    """Words behind the reader's pointer.

    The content map is preferred because it knows the real word counts, but it is
    treated as a floor rather than the answer. A map that lags the reader — routine
    right after a gesture jump onto a page OCR has not merged yet — reports every
    pointer beyond its last sentence as the same total, which would silently cap
    progress and make a moving reader look stalled.

    So the tracker's own tally wins whenever it is higher. Taking the larger of
    the two also means progress never goes backwards mid-session when a merge
    replaces a page with a shorter one.
    """

    if content.sentence_count == 0:
        return snapshot.words_confirmed

    return max(content.words_before(snapshot.pointer), snapshot.words_confirmed)


def _percentage(offset: float, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(min(max(offset / total * 100.0, 0.0), 100.0), 2)


def _ratio(deviation_ms: int, expected_ms: int) -> float:
    """Deviation as a fraction of the expected time.

    Unitless so pages of different lengths are comparable: +0.5 means "took half
    again as long as predicted" whether the page held 50 words or 500.
    """

    if expected_ms <= 0:
        return 0.0
    return round(deviation_ms / expected_ms, 3)
