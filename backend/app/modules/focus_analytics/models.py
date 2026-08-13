"""What the Reading Focus Analysis Engine records and what it concludes.

Two shapes, and the split between them is the point:

    ParagraphObservation   what was observed        counted by the engine
    ParagraphFocus         what it means            concluded by `analysis`

Observations carry no verdict and no derived number. That separation is what lets
the conclusions be re-derived against a corrected baseline without re-running the
session, and it is why the engine can be tested by handing it timestamps rather
than by reading a book at it.

Vocabulary is borrowed, not restated
------------------------------------
`DifficultyLevel` comes from `reading_speed.models` rather than being redefined
here. A second four-valued difficulty enum would be two vocabularies that must
agree, and the first time one grew a level the summaries would start disagreeing
with each other about the same paragraph.

No text, anywhere
-----------------
Nothing here holds a string of the book. A paragraph is a `(page, paragraph)`
pair and a word count; the words themselves stay in Merge Memory, which owns
them. An analytics record that carried text would be a second copy of the book
that nothing was merging.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.app.modules.reading_speed.models import DifficultyLevel

# Weights from the specification: difficulty and meaning requests carry equal
# weight, revisits half of either. Kept as named constants and asserted to sum to
# 1.0 below, because a weighting that silently stopped summing to one would still
# produce plausible scores — just not on the scale anything else assumes.
DIFFICULTY_WEIGHT = 0.40
MEANING_WEIGHT = 0.40
REVISIT_WEIGHT = 0.20

assert abs(DIFFICULTY_WEIGHT + MEANING_WEIGHT + REVISIT_WEIGHT - 1.0) < 1e-9

# What counts as "as bad as it gets" for each component, for normalising to 0-1.
#
# These are scales, not thresholds: a paragraph at the scale value scores full
# marks on that component, and anything worse is clamped. Twice the expected time
# is full difficulty; three meaning requests in one paragraph is a reader who did
# not understand it; two returns to a paragraph is deliberate re-reading rather
# than a stray pointer.
FULL_SCALE_DIFFICULTY_RATIO = 1.0
FULL_SCALE_MEANING_REQUESTS = 3
FULL_SCALE_REVISITS = 2

class ParagraphObservation(BaseModel):
    """What was observed about one paragraph. No conclusions.

    `idle_ms` is a subset of `actual_ms`, not an addition to it: the reader was
    inside this paragraph for `actual_ms`, and for `idle_ms` of that the pointer
    did not move at all. Storing it separately is what makes the difference
    between a hard paragraph and an interrupted one recoverable later.

    `revisits` counts *returns*, so the first visit is not one. A paragraph read
    once has zero.
    """

    model_config = ConfigDict(frozen=True)

    page_index: int = Field(ge=1)
    paragraph_index: int = Field(ge=0)
    words: int = Field(default=0, ge=0)
    actual_ms: int = Field(default=0, ge=0)
    idle_ms: int = Field(default=0, ge=0)
    meaning_requests: int = Field(default=0, ge=0)
    lookups: int = Field(default=0, ge=0)
    revisits: int = Field(default=0, ge=0)

    @property
    def key(self) -> tuple[int, int]:
        return (self.page_index, self.paragraph_index)

    @property
    def focused_ms(self) -> int:
        """Time inside the paragraph with the idle stretches removed.

        This, not `actual_ms`, is what difficulty is measured against. A reader
        who spent four minutes on a paragraph because someone spoke to them for
        three is not a reader who found it hard, and scoring the raw time would
        rank the interruption above every genuinely difficult paragraph in the
        session.
        """

        return max(self.actual_ms - self.idle_ms, 0)


class ParagraphFocus(BaseModel):
    """One paragraph's metrics and the conclusion drawn from them.

    Carries both ratios on purpose. `difficulty_ratio` is the answer to "did this
    take longer than expected once interruptions are discounted?" and drives the
    score; `elapsed_ratio` is the same question against the wall time the reader
    actually spent. When they disagree, the gap *is* the idle time, and a summary
    that showed only one of them would be unable to explain itself.
    """

    model_config = ConfigDict(frozen=True)

    page_index: int = Field(ge=1)
    paragraph_index: int = Field(ge=0)
    words: int = Field(default=0, ge=0)

    expected_ms: int = Field(default=0, ge=0)
    actual_ms: int = Field(default=0, ge=0)
    focused_ms: int = Field(default=0, ge=0)
    idle_ms: int = Field(default=0, ge=0)

    difficulty_ratio: float = 0.0
    elapsed_ratio: float = 0.0

    meaning_requests: int = Field(default=0, ge=0)
    lookups: int = Field(default=0, ge=0)
    revisits: int = Field(default=0, ge=0)

    difficulty: DifficultyLevel = DifficultyLevel.UNKNOWN
    revision_priority: float = Field(default=0.0, ge=0.0, le=100.0)
    evidence: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[int, int]:
        return (self.page_index, self.paragraph_index)

    @property
    def had_idle_time(self) -> bool:
        return self.idle_ms > 0


class FocusReport(BaseModel):
    """Every paragraph one session touched, ranked.

    `needs_attention` is the read that consumers actually want, and it is a
    property rather than a stored field so that it cannot disagree with
    `paragraphs`. Ranking is by priority score, and paragraphs the engine could
    not judge are excluded rather than ranked last — an unjudged paragraph at the
    bottom of a list reads as "this one was fine", which is the one thing the
    UNKNOWN level exists to avoid claiming.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    reader_id: str
    baseline_wpm: float = 0.0
    baseline_was_evidence: bool = False

    paragraphs: tuple[ParagraphFocus, ...] = ()
    total_idle_ms: int = Field(default=0, ge=0)
    total_focused_ms: int = Field(default=0, ge=0)
    total_words: int = Field(default=0, ge=0)

    @property
    def ranked(self) -> tuple[ParagraphFocus, ...]:
        """Judged paragraphs, hardest first. Ties broken by position, not by hash."""

        judged = [p for p in self.paragraphs if p.difficulty is not DifficultyLevel.UNKNOWN]
        return tuple(sorted(judged, key=lambda p: (-p.revision_priority, p.key)))

    def needs_attention(self, *, limit: int = 3, threshold: float = 0.0) -> tuple[
        ParagraphFocus, ...
    ]:
        """The paragraphs worth revisiting. Empty is a valid answer.

        A session where nothing scored above `threshold` returns nothing, rather
        than the least-good paragraph of a session that went fine. Padding this
        list to `limit` would manufacture difficulty on every easy session.
        """

        return tuple(p for p in self.ranked if p.revision_priority > threshold)[:limit]

    @property
    def paragraphs_analysed(self) -> int:
        return len(self.paragraphs)
