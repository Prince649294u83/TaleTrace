"""Reading Speed data contracts.

Pure data only — no logic, no I/O, no state mutation.

Four kinds of shape live here, and the distinction matters more than any single
field:

- `ReadingBaseline` is **permanent**. It survives sessions and changes only on
  deliberate recalibration.
- `ProgressSnapshot` is **temporary**. It describes where a reader is right now
  and dies with the session. It deliberately carries no WPM: a snapshot that
  held its own speed estimate would be a second source of truth competing with
  the baseline.
- `ReadingPrediction` and `SessionAnalytics` are **derived**. Nothing stores
  them; they are computed from the two above plus a content map, so they can
  never drift out of sync with their inputs.
- `ContentMap` is a **description of the text**, supplied by whoever owns it
  (Merge Memory in production, a fake in the simulator).
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from backend.app.modules.audio_engine.models import ReadingPointer


class CalibrationMethod(str, Enum):
    """How a baseline was established.

    Recorded because the number means different things depending on origin: a
    measured passage is evidence, a user-entered value is a claim, and the
    default is a guess. Analytics should not treat them alike.
    """

    MEASURED = "measured"
    MANUAL = "manual"
    DEFAULT = "default"
    ADAPTED = "adapted"


class ReadingMode(str, Enum):
    """What the reader is doing, which decides whether the clock should run."""

    READING = "reading"
    PAUSED = "paused"
    MEANING_MODE = "meaning_mode"
    FINISHED = "finished"


class DifficultyLevel(str, Enum):
    """Inferred difficulty. Never measured directly — always concluded."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


# A plausible adult silent-reading pace, used only until a real measurement
# exists. Explicitly labelled DEFAULT so nothing mistakes it for evidence.
DEFAULT_BASELINE_WPM = 200.0

MIN_PLAUSIBLE_WPM = 40.0
MAX_PLAUSIBLE_WPM = 1200.0


class ReadingBaseline(BaseModel):
    """A reader's established pace. Permanent, and deliberately hard to move.

    Frozen because a baseline that could be mutated in place is a baseline that
    will be mutated mid-session by accident — which is exactly the fluctuation
    this module is built to avoid. Recalibration produces a new instance.

    The bounds are sanity limits, not judgements: below 40 wpm the reader is not
    reading continuously, and above 1200 they are skimming. Values outside that
    range almost always mean a broken timer rather than an unusual reader.
    """

    model_config = ConfigDict(frozen=True)

    reader_id: str
    baseline_wpm: float = Field(ge=MIN_PLAUSIBLE_WPM, le=MAX_PLAUSIBLE_WPM)
    calibrated: bool = False
    method: CalibrationMethod = CalibrationMethod.DEFAULT
    sample_count: int = Field(default=0, ge=0)
    updated_at_ms: int = Field(default=0, ge=0)

    @property
    def words_per_ms(self) -> float:
        """Pace in words per millisecond, the unit every prediction works in."""

        return self.baseline_wpm / 60_000.0

    @property
    def is_evidence(self) -> bool:
        """Whether this baseline came from an actual measurement.

        A default baseline still produces predictions — it has to, or the first
        session would have none — but deviation against a guess is not a signal,
        and analytics uses this to avoid reporting difficulty it cannot support.
        """

        return self.calibrated and self.method in (
            CalibrationMethod.MEASURED,
            CalibrationMethod.ADAPTED,
        )


class ReaderProfile(BaseModel):
    """Everything persistent about one reader.

    Currently just identity plus baseline. It exists as a separate shape because
    preferences (profile choice, voice, font size) will attach to the reader
    rather than to the baseline, and putting them on `ReadingBaseline` would
    force a recalibration to change a font.
    """

    reader_id: str
    display_name: str | None = None
    baseline: ReadingBaseline


class SentenceSpan(BaseModel):
    """One sentence's position and length within a page.

    `word_count` is what predictions consume; the pointer is what everything
    else in TaleTrace already speaks.
    """

    model_config = ConfigDict(frozen=True)

    pointer: ReadingPointer
    word_count: int = Field(ge=0)
    character_count: int = Field(default=0, ge=0)


class ContentMap(BaseModel):
    """How much text there is and where.

    Built from Merge Memory, never by this module. Reading Speed reads content
    structure and never touches the text itself, which is why this carries
    counts and pointers but no strings.

    Sentences are assumed to be in reading order. `page_word_counts` is kept
    alongside the spans because remaining-page predictions need per-page totals
    far more often than they need individual sentences.
    """

    model_config = ConfigDict(frozen=True)

    sentences: tuple[SentenceSpan, ...] = ()
    page_word_counts: dict[int, int] = Field(default_factory=dict)
    source_version: int = 0

    @property
    def total_words(self) -> int:
        return sum(span.word_count for span in self.sentences)

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)

    def words_before(self, pointer: ReadingPointer) -> int:
        """Words in every sentence preceding `pointer`.

        Compares on `sentence_order_key()` rather than list index, so a pointer
        that does not correspond to any known sentence still lands in the right
        place. This is the normal case after a gesture jump: the pointer is real
        even when the map is a page behind.
        """

        key = pointer.sentence_order_key()
        return sum(
            span.word_count
            for span in self.sentences
            if span.pointer.sentence_order_key() < key
        )

    def index_of(self, pointer: ReadingPointer) -> int | None:
        """Position of `pointer` in reading order, or None if absent."""

        key = pointer.sentence_order_key()
        for index, span in enumerate(self.sentences):
            if span.pointer.sentence_order_key() == key:
                return index
        return None

    def pointer_at_word(self, word_offset: int) -> ReadingPointer | None:
        """The sentence containing the word at `word_offset`.

        Used to turn "the reader should be about 340 words in" into a sentence a
        human can be shown. Returns the last sentence when the offset runs past
        the end, since a prediction beyond the content still points somewhere
        real.
        """

        if not self.sentences:
            return None

        consumed = 0
        for span in self.sentences:
            consumed += span.word_count
            if word_offset < consumed:
                return span.pointer
        return self.sentences[-1].pointer


class ProgressSnapshot(BaseModel):
    """Where a reader is, right now. Temporary, and carries no pace.

    The absence of a WPM field is the whole point. Progress is *observed*;
    pace is *established*. Mixing them is how adaptive systems end up with a
    speed estimate that chases itself.

    Two clocks, mirroring the audio engine's split: `elapsed_reading_ms`
    excludes paused and Meaning Mode intervals, so looking up a word never makes
    the reader appear slower; `elapsed_wall_ms` is total time in the session.
    """

    session_id: str
    reader_id: str
    pointer: ReadingPointer = Field(default_factory=ReadingPointer)
    mode: ReadingMode = ReadingMode.READING
    elapsed_reading_ms: int = Field(default=0, ge=0)
    elapsed_wall_ms: int = Field(default=0, ge=0)
    words_confirmed: int = Field(default=0, ge=0)
    pages_visited: int = Field(default=0, ge=0)
    lookup_count: int = Field(default=0, ge=0)
    meaning_mode_count: int = Field(default=0, ge=0)
    pointer_corrections: int = Field(default=0, ge=0)
    page_revisits: int = Field(default=0, ge=0)

    @property
    def is_clock_running(self) -> bool:
        return self.mode is ReadingMode.READING


class ReadingPrediction(BaseModel):
    """Where the reader *would* be at their established pace.

    Derived, never stored. Everything here is a comparison against the baseline;
    none of it is an instruction. No consumer should change playback, pointer,
    or content because of a prediction.

    `confidence` is low when the baseline is a guess or too little time has
    passed to mean anything. A prediction is always produced — a UI needs
    something to show from second one — but low confidence marks the ones that
    should not drive analytics or be shown as fact.
    """

    session_id: str
    expected_pointer: ReadingPointer | None = None
    expected_word_offset: int = Field(default=0, ge=0)
    actual_word_offset: int = Field(default=0, ge=0)
    expected_sentence_index: int | None = None
    actual_sentence_index: int | None = None
    words_remaining: int = Field(default=0, ge=0)
    expected_finish_ms: int | None = Field(default=None, ge=0)
    expected_page_finish_ms: int | None = Field(default=None, ge=0)
    progress_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    expected_progress_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    deviation_words: int = 0
    deviation_ms: int = 0
    deviation_ratio: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    baseline_wpm: float = 0.0

    @property
    def is_ahead(self) -> bool:
        """Reader is further along than the baseline predicts."""

        return self.deviation_words > 0

    @property
    def is_behind(self) -> bool:
        return self.deviation_words < 0


class DifficultyMetrics(BaseModel):
    """Evidence for one page, and the conclusion drawn from it.

    The evidence is kept beside the verdict on purpose. Time alone cannot
    distinguish a hard page from an interrupted one — a reader who set the book
    down looks identical to one who struggled — so the lookups, re-reads and
    corrections that justify the verdict travel with it.
    """

    page_index: int
    words: int = Field(default=0, ge=0)
    expected_ms: int = Field(default=0, ge=0)
    actual_ms: int = Field(default=0, ge=0)
    deviation_ms: int = 0
    deviation_ratio: float = 0.0
    lookups: int = Field(default=0, ge=0)
    meaning_requests: int = Field(default=0, ge=0)
    pointer_corrections: int = Field(default=0, ge=0)
    revisits: int = Field(default=0, ge=0)
    difficulty: DifficultyLevel = DifficultyLevel.UNKNOWN
    evidence: tuple[str, ...] = ()


class SessionAnalytics(BaseModel):
    """What a session revealed, computed after the fact.

    Distinct from `PlaybackStatistics` from the audio engine, and the distinction
    is the point: these fields measure the reader — words covered, over the
    reader's reading clock — while `PlaybackStatistics` measures narration, in
    sentences and words spoken aloud. Neither substitutes for the other. A reader
    who listened to a whole page while pointing at four words covered a page and
    was read four words, and both numbers are true.

    So `words_read` is reading progress whether narration was on or off, and
    `tts_assisted` records only *that* it was on — a caveat when comparing the
    pace, because TTS paces the reader instead of the reader pacing themselves,
    not a switch that changes how the pace is computed.

    `reading_duration_ms` is the reader's reading clock with pauses and Meaning
    Mode excluded (`ProgressSnapshot.elapsed_reading_ms`, or the observations'
    sum when the snapshot never advanced); `wall_duration_ms` is the whole
    session. Neither is ever `PlaybackStatistics.reading_time_ms`, which counts
    time spent *speaking*. A single field meaning "reading time" when narration
    is off and "speaking time" when it is on is unreadable everywhere
    downstream — the baseline `apply_suggested_baseline` adapts from, the
    minutes the website prints, the difficulty each page is judged against. The
    whole contract, in one place:

        words_read           reader    — pointer-confirmed progress
        reading_duration_ms  reader    — progress clock, pauses excluded
        wall_duration_ms     reader    — whole session
        words_spoken         narration — `PlaybackStatistics`
        reading_time_ms      narration — `PlaybackStatistics`, speaking clock
        tts_assisted         metadata  — narration was on

    No playback-duration field is mirrored here. Nothing on the website or the
    rig asks for one, and `PlaybackStatistics` already carries both its clocks
    for whoever wants them.
    """

    session_id: str
    reader_id: str
    baseline_wpm: float = 0.0
    session_wpm: float = 0.0
    words_read: int = Field(default=0, ge=0)
    pages_read: int = Field(default=0, ge=0)
    reading_duration_ms: int = Field(default=0, ge=0)
    wall_duration_ms: int = Field(default=0, ge=0)
    average_deviation_ratio: float = 0.0
    lookup_count: int = Field(default=0, ge=0)
    meaning_requests: int = Field(default=0, ge=0)
    hardest_page: int | None = None
    easiest_page: int | None = None
    pages: tuple[DifficultyMetrics, ...] = ()
    tts_assisted: bool = False
    baseline_was_evidence: bool = False
    suggested_baseline_wpm: float | None = None

    @property
    def pace_vs_baseline(self) -> float:
        """Session pace as a fraction of baseline. 1.0 means exactly on pace."""

        if self.baseline_wpm <= 0:
            return 0.0
        return self.session_wpm / self.baseline_wpm
