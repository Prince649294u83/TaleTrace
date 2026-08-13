"""Turning paragraph observations into conclusions. Pure functions only.

Nothing here holds state, reads a clock, or knows a session exists. The engine
observes; this decides what the observations mean. That split is why a corrected
baseline can be applied to a finished session without replaying it, and why every
threshold below is testable by calling one function with one dataclass.

The question this module answers
--------------------------------
Not "was the reader distracted?" — that question is unanswerable from a pointer
and a clock, and answering it anyway would mean telling readers something about
themselves that the data does not support. The question is:

    which sections required more attention than expected?

Every threshold below exists to keep the answer to *that* question honest. A
paragraph is only reported as difficult when something beyond elapsed time says
so, because elapsed time cannot distinguish a hard paragraph from an interrupted
one — that is the same finding `reading_speed.analytics` records at page
granularity, and it does not become less true at paragraph granularity. It
becomes *more* true: a paragraph is a tenth the size of a page, so the noise in
its timing is proportionally larger and a lone slow measurement means less.

Thresholds are imported where they already exist
------------------------------------------------
`SLOW_RATIO`, `VERY_SLOW_RATIO` and `FAST_RATIO` come from
`reading_speed.analytics` rather than being restated. They are scale-free — a
ratio means the same thing whatever the unit of text — so a second copy here
would be two numbers that must agree, and the first time one was tuned the two
engines would start disagreeing about the same reading.

The friction counts are *not* imported, and that is deliberate: three lookups on
a page is a reader working hard, while three lookups in one paragraph is a reader
who did not understand it. Same evidence, different denominator.
"""

from __future__ import annotations

from backend.app.modules.focus_analytics.models import (
    DIFFICULTY_WEIGHT,
    FULL_SCALE_DIFFICULTY_RATIO,
    FULL_SCALE_MEANING_REQUESTS,
    FULL_SCALE_REVISITS,
    MEANING_WEIGHT,
    REVISIT_WEIGHT,
    ParagraphFocus,
    ParagraphObservation,
)
from backend.app.modules.reading_speed.analytics import (
    FAST_RATIO,
    SLOW_RATIO,
    VERY_SLOW_RATIO,
)
from backend.app.modules.reading_speed.models import DifficultyLevel, ReadingBaseline
from backend.app.modules.reading_speed.predictor import expected_ms_for

# Paragraph-scale friction counts. One meaning request in a single paragraph is
# already a signal; two is a paragraph the reader did not follow. The page-level
# equivalents are 1 and 3, over roughly ten times as much text.
FRICTION_FOR_MEDIUM = 1
FRICTION_FOR_HIGH = 2

# Below this, a paragraph is too short for its timing to mean anything. A heading
# or a one-line fragment read in 300ms is not evidence of a fast reader.
MIN_PARAGRAPH_WORDS = 8

# Below this, the reader crossed the paragraph rather than read it — a gesture
# jump, or a pointer dragged through. Without this floor such a paragraph returns
# a large negative ratio, falls through every slow branch, and is reported LOW
# "faster than predicted", which sounds like a verdict and is really its absence.
MIN_MEASURABLE_FOCUSED_MS = 500

# The idle rule. A gap in pointer movement is only idle time once it exceeds a
# generous allowance for the text actually crossed: three times what the reader's
# own baseline predicts, and never less than five seconds. Five because pointer
# updates arrive on gestures and page merges rather than on a timer, so gaps of a
# second or two are the normal texture of a session and not the reader stopping.
IDLE_ALLOWANCE_MULTIPLE = 3.0
IDLE_FLOOR_MS = 5_000


def idle_ms_for_gap(gap_ms: int, words_crossed: int, baseline: ReadingBaseline) -> int:
    """The part of a `gap_ms` pause that counts as idle. Zero for ordinary gaps.

    Only the *excess* over the allowance is counted, not the whole gap. A reader
    who paused eight seconds where six were reasonable was idle for two, and
    charging them all eight would make every slightly slow paragraph look
    abandoned.

    Deliberately continuous rather than a flag: idle time is a quantity that gets
    subtracted from a measurement, and a boolean "was idle" would force the caller
    to either discard the whole paragraph or keep all of it.
    """

    if gap_ms <= 0:
        return 0

    allowance = max(
        expected_ms_for(max(words_crossed, 0), baseline) * IDLE_ALLOWANCE_MULTIPLE,
        float(IDLE_FLOOR_MS),
    )
    return max(int(round(gap_ms - allowance)), 0)


def revision_priority(
    *,
    difficulty_ratio: float,
    meaning_requests: int,
    revisits: int,
) -> float:
    """Revision Priority Score, 0-100. The specification's 40/40/20 weighting.

    Each component is normalised to 0-1 against a full-scale value before being
    weighted, so the three are commensurable: without that, "one meaning request"
    and "a ratio of 1.0" would enter the sum on unrelated scales and whichever
    happened to be numerically larger would dominate the score.

    A paragraph read faster than expected contributes zero difficulty rather than
    a negative amount. Reading something quickly is not evidence that it needs
    less revision than a paragraph with no signal at all, and letting it go
    negative would let speed cancel out real meaning requests.

    `lookups` is not a term here. A completed lookup is the *resolution* of a
    meaning request, not a separate event, so counting both would score the same
    moment of confusion twice. It travels in the evidence instead.
    """

    difficulty = _clamp01(difficulty_ratio / FULL_SCALE_DIFFICULTY_RATIO)
    meaning = _clamp01(meaning_requests / FULL_SCALE_MEANING_REQUESTS)
    revisited = _clamp01(revisits / FULL_SCALE_REVISITS)

    score = (
        DIFFICULTY_WEIGHT * difficulty + MEANING_WEIGHT * meaning + REVISIT_WEIGHT * revisited
    )
    return round(100.0 * score, 1)


def assess_paragraph(
    observation: ParagraphObservation, baseline: ReadingBaseline
) -> ParagraphFocus:
    """One paragraph's metrics, verdict and priority score.

    Difficulty is measured against `focused_ms` — elapsed time with the idle
    stretches removed — while `elapsed_ratio` reports the same comparison against
    the raw time. Both are returned because when they disagree the difference is
    exactly the idle time, and a report that showed only the raw figure would rank
    an interruption above every genuinely hard paragraph in the session.
    """

    expected_ms = expected_ms_for(observation.words, baseline)
    focused_ms = observation.focused_ms

    difficulty_ratio = _ratio(focused_ms, expected_ms)
    elapsed_ratio = _ratio(observation.actual_ms, expected_ms)

    level, evidence = _classify(observation, baseline, expected_ms, difficulty_ratio)

    return ParagraphFocus(
        page_index=observation.page_index,
        paragraph_index=observation.paragraph_index,
        words=observation.words,
        expected_ms=expected_ms,
        actual_ms=observation.actual_ms,
        focused_ms=focused_ms,
        idle_ms=observation.idle_ms,
        difficulty_ratio=difficulty_ratio,
        elapsed_ratio=elapsed_ratio,
        meaning_requests=observation.meaning_requests,
        lookups=observation.lookups,
        revisits=observation.revisits,
        difficulty=level,
        revision_priority=revision_priority(
            difficulty_ratio=difficulty_ratio,
            meaning_requests=observation.meaning_requests,
            revisits=observation.revisits,
        ),
        evidence=evidence,
    )


def _classify(
    observation: ParagraphObservation,
    baseline: ReadingBaseline,
    expected_ms: int,
    ratio: float,
) -> tuple[DifficultyLevel, tuple[str, ...]]:
    """Pick a level and collect the reasons, in the order a reader would read them.

    Friction is counted before time is, because friction is the only signal that
    does not need a baseline to mean something: a reader who asked what a word
    meant asked, whatever their pace. Time is corroboration.
    """

    evidence: list[str] = []
    if observation.meaning_requests:
        evidence.append(f"{observation.meaning_requests} meaning request(s)")
    if observation.lookups:
        evidence.append(f"{observation.lookups} word lookup(s)")
    if observation.revisits:
        evidence.append(f"returned to this paragraph {observation.revisits} time(s)")
    if observation.idle_ms:
        # Wording is fixed by the specification and is not a style choice. The
        # data supports "the pointer did not move for a while"; it does not
        # support any claim about what the reader was doing instead.
        evidence.append(
            f"possible idle time of {observation.idle_ms / 1000:.1f}s — "
            "reading paused longer than expected"
        )

    friction = observation.meaning_requests + observation.revisits

    if not baseline.is_evidence:
        return _unmeasurable(friction, evidence, "baseline is not a measurement")
    if observation.words < MIN_PARAGRAPH_WORDS:
        return _unmeasurable(
            friction, evidence, f"only {observation.words} word(s) in the paragraph"
        )
    if expected_ms <= 0:
        return _unmeasurable(friction, evidence, "no expected reading time")
    if observation.focused_ms < MIN_MEASURABLE_FOCUSED_MS:
        return _unmeasurable(
            friction,
            evidence,
            f"only {observation.focused_ms}ms of reading — crossed, not read",
        )

    if ratio >= VERY_SLOW_RATIO:
        evidence.insert(0, f"{ratio:+.0%} longer than expected")
        if friction >= FRICTION_FOR_HIGH:
            return DifficultyLevel.HIGH, tuple(evidence)
        if friction >= FRICTION_FOR_MEDIUM:
            return DifficultyLevel.MEDIUM, tuple(evidence)
        # The whole philosophy of the module, in one branch. Slow with nothing
        # else is not difficulty, and saying otherwise would report every
        # interruption in the session as a comprehension problem.
        evidence.append("no meaning requests or re-reads — attention, not difficulty, unclear")
        return DifficultyLevel.UNKNOWN, tuple(evidence)

    if ratio >= SLOW_RATIO:
        evidence.insert(0, f"{ratio:+.0%} longer than expected")
        if friction >= FRICTION_FOR_MEDIUM:
            return DifficultyLevel.MEDIUM, tuple(evidence)
        evidence.append("mild slowdown with no other signals")
        return DifficultyLevel.LOW, tuple(evidence)

    if ratio <= FAST_RATIO and friction == 0:
        return DifficultyLevel.LOW, (f"{ratio:+.0%} faster than expected, no lookups",)

    if friction >= FRICTION_FOR_HIGH:
        evidence.insert(0, "read at the expected pace, but with repeated lookups")
        return DifficultyLevel.MEDIUM, tuple(evidence)

    evidence.insert(0, f"within {abs(ratio):.0%} of the expected time")
    return DifficultyLevel.LOW, tuple(evidence)


def _unmeasurable(
    friction: int, evidence: list[str], reason: str
) -> tuple[DifficultyLevel, tuple[str, ...]]:
    """No usable timing. Friction can still convict; silence cannot.

    A paragraph whose timing is unusable is not automatically a paragraph with
    nothing to say: two meaning requests in a four-word heading is a reader who
    stopped on it, and reporting UNKNOWN there would drop the clearest signal the
    session produced. What it can never be is HIGH — that verdict needs both
    halves of the evidence.
    """

    if friction >= FRICTION_FOR_HIGH:
        evidence.append(f"rated on lookups alone: {reason}")
        return DifficultyLevel.MEDIUM, tuple(evidence)

    evidence.append(reason)
    return DifficultyLevel.UNKNOWN, tuple(evidence)


def _ratio(actual_ms: int, expected_ms: int) -> float:
    """Reading Difficulty: (actual - expected) / expected. Zero means on pace."""

    if expected_ms <= 0:
        return 0.0
    return round((actual_ms - expected_ms) / expected_ms, 3)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
