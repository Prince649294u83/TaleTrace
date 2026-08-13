"""Reading Focus Analysis Engine.

Two kinds of test here, and the second kind is the point.

The first kind checks the arithmetic: expected time, actual time, the difficulty
ratio, the 40/40/20 score. Those are worth pinning but they are not where this
engine can go wrong.

The second kind checks the *philosophy* — that a slow paragraph with no friction
is never reported as difficult, that idle time is worded as a possibility and
never as a claim about the reader, that the engine cannot reach into any other
module. Those constraints are the specification. A version of this engine that
passed every arithmetic test and failed those would be the distraction detector
the specification exists to prevent.
"""

from __future__ import annotations

import pytest

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.focus_analytics import (
    DIFFICULTY_WEIGHT,
    FULL_SCALE_MEANING_REQUESTS,
    FULL_SCALE_REVISITS,
    IDLE_FLOOR_MS,
    MEANING_WEIGHT,
    REVISIT_WEIGHT,
    FocusAnalyticsEngine,
    ParagraphObservation,
    assess_paragraph,
    idle_ms_for_gap,
    revision_priority,
)
from backend.app.modules.merge_memory.engine import MergeMemory
from backend.app.modules.reading_speed.models import (
    CalibrationMethod,
    DifficultyLevel,
    ReadingBaseline,
)
from backend.app.shared.clock import VirtualClock
from backend.app.shared.events import SessionEvent
from backend.app.shared.merge_memory import MergeMemorySource

# A measured baseline, because most branches under test require one: analysis
# refuses to rate difficulty against a default, and rightly so.
MEASURED = ReadingBaseline(
    reader_id="reader",
    baseline_wpm=200.0,
    calibrated=True,
    method=CalibrationMethod.MEASURED,
    sample_count=3,
)
DEFAULTED = ReadingBaseline(reader_id="reader", baseline_wpm=200.0)

# 200 wpm is 300ms a word, which makes every expected time in these tests exact
# arithmetic rather than a rounding argument.
MS_PER_WORD = 300


def observation(**kwargs) -> ParagraphObservation:
    """A paragraph observation with defaults that are on pace and frictionless."""

    base = {
        "page_index": 1,
        "paragraph_index": 0,
        "words": 40,
        "actual_ms": 40 * MS_PER_WORD,
        "idle_ms": 0,
    }
    base.update(kwargs)
    return ParagraphObservation(**base)


def memory_with(paragraphs: list[str], *, page_index: int = 1) -> MergeMemory:
    """A real MergeMemory holding `paragraphs`, not a fake.

    The engine's word counts come from `content_map()`, which goes through the
    Audio Engine's sentence segmenter. A stub returning invented counts would test
    the engine against a paragraph structure the product never produces.
    """

    memory = MergeMemory()
    memory.begin_page(page_index)
    memory.apply_frame("\n\n".join(paragraphs), page_index=page_index)
    memory.commit_page(page_index)
    return memory


def words(count: int, *, start: int = 0) -> str:
    """A paragraph of `count` distinct words, ending in a full stop."""

    return " ".join(f"w{start + i}" for i in range(count)) + "."


class TestItIsNotADistractionDetector:
    """The specification's prohibitions, as tests.

    These come first because they are the constraints most easily lost to a later
    "improvement": every one of them is a place where the obvious change makes the
    engine claim more than the data supports.
    """

    def test_a_slow_paragraph_with_no_friction_is_never_reported_as_difficult(self):
        # Three times the expected time, and nothing else. This is the reader who
        # set the book down, and the engine must not call it difficulty.
        result = assess_paragraph(
            observation(actual_ms=40 * MS_PER_WORD * 3), MEASURED
        )

        assert result.difficulty is DifficultyLevel.UNKNOWN
        assert result.difficulty_ratio > 1.0
        assert any("not difficulty" in reason for reason in result.evidence)

    def test_the_same_slowness_with_friction_is_reported_as_difficult(self):
        # The identical timing, plus evidence that the reader was working at it.
        # Only the friction differs, and it is what makes the verdict possible.
        result = assess_paragraph(
            observation(actual_ms=40 * MS_PER_WORD * 3, meaning_requests=2), MEASURED
        )

        assert result.difficulty is DifficultyLevel.HIGH

    def test_idle_time_is_worded_as_a_possibility_not_as_a_claim(self):
        result = assess_paragraph(observation(actual_ms=60_000, idle_ms=30_000), MEASURED)

        idle_evidence = [r for r in result.evidence if "idle" in r.lower()]
        assert idle_evidence, "idle time was recorded but never explained"
        assert any("possible idle time" in r.lower() for r in idle_evidence)
        assert any("paused longer than expected" in r.lower() for r in idle_evidence)

    def test_no_evidence_string_anywhere_accuses_the_reader(self):
        # Swept across the branches rather than asserted on one, because the
        # forbidden vocabulary could be introduced in any of them.
        forbidden = ("distract", "attention span", "wandering", "not paying")
        cases = [
            observation(),
            observation(actual_ms=40 * MS_PER_WORD * 3),
            observation(actual_ms=60_000, idle_ms=40_000),
            observation(meaning_requests=3, lookups=2, revisits=2),
            observation(words=2, actual_ms=400),
            observation(actual_ms=200),
        ]
        for case in cases:
            for baseline in (MEASURED, DEFAULTED):
                for reason in assess_paragraph(case, baseline).evidence:
                    lowered = reason.lower()
                    for word in forbidden:
                        assert word not in lowered, f"{reason!r} accuses the reader"

    def test_no_module_is_named_for_distraction(self):
        import backend.app.modules.focus_analytics as package

        assert "distract" not in package.__name__.lower()
        for name in dir(package):
            assert "distract" not in name.lower()

    def test_the_engine_exposes_nothing_that_could_control_another_module(self):
        # Structural, not aspirational: the engine having no setters is what makes
        # "observes only" a fact about the code rather than a promise in a comment.
        public = [name for name in dir(FocusAnalyticsEngine) if not name.startswith("_")]
        for name in public:
            assert not name.startswith("set_"), name
        for forbidden in ("advance", "seek", "speak", "pause_playback", "move_pointer"):
            assert forbidden not in public


class TestTheMetricsTheSpecificationNames:
    def test_expected_reading_time_is_words_over_the_baseline(self):
        result = assess_paragraph(observation(words=100, actual_ms=30_000), MEASURED)

        # 100 words at 200 wpm is exactly 30 seconds.
        assert result.expected_ms == 30_000
        assert result.actual_ms == 30_000
        assert result.difficulty_ratio == 0.0

    def test_reading_difficulty_is_actual_minus_expected_over_expected(self):
        result = assess_paragraph(observation(words=100, actual_ms=45_000), MEASURED)

        assert result.expected_ms == 30_000
        assert result.difficulty_ratio == pytest.approx(0.5)

    def test_difficulty_is_measured_against_focused_time_not_elapsed_time(self):
        # A paragraph that took twice as long, half of it idle. The reading itself
        # was on pace, and that is what the verdict has to be about.
        result = assess_paragraph(
            observation(words=100, actual_ms=60_000, idle_ms=30_000), MEASURED
        )

        assert result.focused_ms == 30_000
        assert result.difficulty_ratio == pytest.approx(0.0)
        assert result.elapsed_ratio == pytest.approx(1.0)
        assert result.had_idle_time

    def test_idle_time_never_exceeds_the_time_in_the_paragraph(self):
        result = assess_paragraph(observation(actual_ms=5_000, idle_ms=99_000), MEASURED)

        assert result.focused_ms == 0


class TestTheIdleRule:
    def test_an_ordinary_gap_is_not_idle_time(self):
        # Twenty words at 200 wpm is 6s expected; 8s is slow, not abandoned.
        assert idle_ms_for_gap(8_000, words_crossed=20, baseline=MEASURED) == 0

    def test_only_the_excess_over_the_allowance_is_counted(self):
        # 20 words -> 6s expected -> 18s allowed. A 25s gap is 7s of idle, not 25.
        assert idle_ms_for_gap(25_000, words_crossed=20, baseline=MEASURED) == 7_000

    def test_a_long_pause_over_no_text_is_idle_past_the_floor(self):
        assert idle_ms_for_gap(IDLE_FLOOR_MS - 1, words_crossed=0, baseline=MEASURED) == 0
        assert idle_ms_for_gap(IDLE_FLOOR_MS + 3_000, words_crossed=0, baseline=MEASURED) == 3_000

    def test_the_allowance_scales_with_the_text_crossed(self):
        # The same pause is idle across three words and unremarkable across three
        # hundred. A flat threshold would report every dense paragraph as idle.
        assert idle_ms_for_gap(30_000, words_crossed=3, baseline=MEASURED) > 0
        assert idle_ms_for_gap(30_000, words_crossed=300, baseline=MEASURED) == 0

    def test_a_negative_or_zero_gap_is_never_idle(self):
        assert idle_ms_for_gap(0, words_crossed=10, baseline=MEASURED) == 0
        assert idle_ms_for_gap(-500, words_crossed=10, baseline=MEASURED) == 0


class TestTheRevisionPriorityScore:
    def test_a_frictionless_on_pace_paragraph_scores_zero(self):
        assert revision_priority(difficulty_ratio=0.0, meaning_requests=0, revisits=0) == 0.0

    def test_everything_at_full_scale_scores_one_hundred(self):
        score = revision_priority(
            difficulty_ratio=1.0,
            meaning_requests=FULL_SCALE_MEANING_REQUESTS,
            revisits=FULL_SCALE_REVISITS,
        )
        assert score == 100.0

    def test_the_weights_are_forty_forty_twenty(self):
        difficulty_only = revision_priority(difficulty_ratio=1.0, meaning_requests=0, revisits=0)
        meaning_only = revision_priority(
            difficulty_ratio=0.0, meaning_requests=FULL_SCALE_MEANING_REQUESTS, revisits=0
        )
        revisits_only = revision_priority(
            difficulty_ratio=0.0, meaning_requests=0, revisits=FULL_SCALE_REVISITS
        )

        assert difficulty_only == pytest.approx(100.0 * DIFFICULTY_WEIGHT)
        assert meaning_only == pytest.approx(100.0 * MEANING_WEIGHT)
        assert revisits_only == pytest.approx(100.0 * REVISIT_WEIGHT)
        # Difficulty and meaning requests carry equal weight, which is the part of
        # the weighting most likely to be "improved" by someone who assumes time
        # matters more than what the reader asked for.
        assert difficulty_only == meaning_only

    def test_every_component_is_clamped_rather_than_allowed_to_run_away(self):
        # Ten meaning requests in one paragraph is not three times worse than
        # three. Without the clamp one pathological paragraph would flatten every
        # other score in the session to nothing.
        assert revision_priority(difficulty_ratio=50.0, meaning_requests=99, revisits=99) == 100.0

    def test_reading_faster_than_expected_does_not_cancel_out_friction(self):
        # A negative difficulty ratio must contribute zero, not a negative amount:
        # a reader who skimmed a paragraph and asked what two words meant still
        # asked what two words meant.
        fast = revision_priority(difficulty_ratio=-0.9, meaning_requests=2, revisits=0)
        on_pace = revision_priority(difficulty_ratio=0.0, meaning_requests=2, revisits=0)

        assert fast == on_pace
        assert fast > 0.0

    def test_the_score_stays_inside_zero_to_one_hundred_across_wide_input(self):
        for ratio in (-5.0, -0.5, 0.0, 0.3, 1.0, 4.0, 100.0):
            for meaning in (0, 1, 5, 50):
                for revisits in (0, 1, 9):
                    score = revision_priority(
                        difficulty_ratio=ratio, meaning_requests=meaning, revisits=revisits
                    )
                    assert 0.0 <= score <= 100.0


class TestWhatCannotBeJudged:
    def test_a_short_paragraph_is_unknown_rather_than_fast(self):
        result = assess_paragraph(observation(words=3, actual_ms=600), MEASURED)

        assert result.difficulty is DifficultyLevel.UNKNOWN
        assert any("word(s) in the paragraph" in r for r in result.evidence)

    def test_a_paragraph_crossed_rather_than_read_is_unknown(self):
        result = assess_paragraph(observation(words=40, actual_ms=100), MEASURED)

        assert result.difficulty is DifficultyLevel.UNKNOWN
        assert any("crossed, not read" in r for r in result.evidence)

    def test_a_default_baseline_cannot_support_a_difficulty_verdict(self):
        result = assess_paragraph(observation(actual_ms=40 * MS_PER_WORD * 3), DEFAULTED)

        assert result.difficulty is DifficultyLevel.UNKNOWN
        assert any("not a measurement" in r for r in result.evidence)

    def test_friction_alone_still_convicts_when_the_timing_is_unusable(self):
        # A four-word heading the reader asked about twice. The timing means
        # nothing and the friction means everything, and reporting UNKNOWN here
        # would drop the clearest signal in the session.
        result = assess_paragraph(
            observation(words=4, actual_ms=800, meaning_requests=2), MEASURED
        )

        assert result.difficulty is DifficultyLevel.MEDIUM
        assert any("lookups alone" in r for r in result.evidence)

    def test_friction_alone_never_reaches_the_highest_verdict(self):
        result = assess_paragraph(
            observation(words=4, actual_ms=800, meaning_requests=9, revisits=9), MEASURED
        )

        assert result.difficulty is DifficultyLevel.MEDIUM


class TestTheEngineObservesASession:
    """Driven by injected timestamps. No hardware, no sleeping, no real clock."""

    def build(self, paragraphs: list[str]) -> tuple[FocusAnalyticsEngine, VirtualClock]:
        clock = VirtualClock()
        engine = FocusAnalyticsEngine(
            "session",
            "reader",
            memory=memory_with(paragraphs),
            baseline=MEASURED,
            clock=clock.now,
        )
        return engine, clock

    def test_it_asks_merge_memory_for_word_counts_rather_than_counting_text(self):
        engine, clock = self.build([words(40), words(60, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(18.0)
        engine.session_finished()

        report = engine.report()
        assert [p.words for p in report.paragraphs] == [40, 60]
        # Expected times follow from the counts the memory reported, which is the
        # whole of "never reconstruct text": the engine held no strings at all.
        assert [p.expected_ms for p in report.paragraphs] == [12_000, 18_000]

    def test_time_is_attributed_to_the_paragraph_the_reader_was_in(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(20.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(5.0)
        engine.session_finished()

        first, second = engine.report().paragraphs
        assert first.actual_ms == 20_000
        assert second.actual_ms == 5_000

    def test_a_paragraph_returned_to_counts_as_a_revisit_not_as_a_new_paragraph(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(6.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(6.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(6.0)
        engine.session_finished()

        report = engine.report()
        assert report.paragraphs_analysed == 2
        first = next(p for p in report.paragraphs if p.paragraph_index == 0)
        assert first.revisits == 1
        # Both visits are credited to the same paragraph, so a re-read shows up as
        # more time on the text rather than as a second entry that halves it.
        assert first.actual_ms == 12_000

    def test_meaning_requests_land_on_the_paragraph_being_read(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(4.0)
        engine.meaning_requested("recalcitrant")
        engine.lookup_completed("recalcitrant")
        engine.meaning_mode_off()
        clock.advance(4.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(12.0)
        engine.session_finished()

        first, second = engine.report().paragraphs
        assert (first.meaning_requests, first.lookups) == (1, 1)
        assert (second.meaning_requests, second.lookups) == (0, 0)

    def test_meaning_mode_does_not_make_the_paragraph_look_slower(self):
        # The reader spends 12s reading and 60s in Meaning Mode. The paragraph
        # took 12s of reading, and charging the lookup as reading time too would
        # score the same moment of confusion twice.
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.meaning_requested()
        clock.advance(60.0)
        engine.meaning_mode_off()
        engine.session_finished()

        only = engine.report().paragraphs[0]
        assert only.actual_ms == 12_000
        assert only.idle_ms == 0
        assert only.difficulty_ratio == pytest.approx(0.0)

    def test_an_explicit_pause_is_not_idle_time(self):
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(6.0)
        engine.session_paused()
        clock.advance(600.0)
        engine.session_resumed()
        clock.advance(6.0)
        engine.session_finished()

        only = engine.report().paragraphs[0]
        assert only.idle_ms == 0
        assert only.actual_ms == 12_000

    def test_a_pause_does_not_forgive_a_stall_that_preceded_it(self):
        # The other half of `test_an_explicit_pause_is_not_idle_time`, and the
        # failure mode that hid behind it: a pause must not *subtract* idle time
        # either. `_resume` once reset the gap window to the moment of resuming,
        # which erased whatever stall had already accrued — so the way to keep a
        # stall out of the report was to pause afterwards, and every reader who
        # drifted off and then deliberately took a break came back to a clean one.
        stalled_then_paused, clock = self.build([words(40)])
        engine = stalled_then_paused

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(120.0)  # the stall
        engine.session_paused()
        clock.advance(600.0)  # the break, which is not idle time
        engine.session_resumed()
        engine.session_finished()

        straight, straight_clock = self.build([words(40)])
        straight.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        straight_clock.advance(120.0)  # the same stall, no break after it
        straight.session_finished()

        paused_idle = engine.report().paragraphs[0].idle_ms
        assert paused_idle == straight.report().paragraphs[0].idle_ms
        assert paused_idle > 0

    def test_a_long_unexplained_stall_becomes_possible_idle_time(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        # Two minutes without a pointer move, over a paragraph worth twelve
        # seconds. Nobody paused and nobody asked anything.
        clock.advance(120.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(12.0)
        engine.session_finished()

        report = engine.report()
        first = report.paragraphs[0]
        assert first.idle_ms > 0
        assert report.total_idle_ms == first.idle_ms
        # And with no friction to corroborate it, the verdict stays UNKNOWN.
        assert first.difficulty is DifficultyLevel.UNKNOWN

    def test_a_stall_on_the_last_paragraph_is_still_idle_time(self):
        # No pointer move ever follows the last paragraph, so the gap that reveals
        # idle time everywhere else never arrives here. Without an explicit close
        # the last paragraph of every interrupted session is the slowest thing in
        # the report.
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(180.0)
        engine.session_finished()

        only = engine.report().paragraphs[0]
        assert only.actual_ms == 180_000
        assert only.idle_ms > 0
        assert only.focused_ms < only.actual_ms
        assert only.difficulty is DifficultyLevel.UNKNOWN

    def test_finishing_normally_does_not_invent_idle_time(self):
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.session_finished()

        assert engine.report().paragraphs[0].idle_ms == 0

    def test_a_page_turn_closes_the_paragraph_on_the_page_being_left(self):
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.page_changed(ReadingPointer(page_index=2, paragraph_index=0))
        clock.advance(9.0)
        engine.session_finished()

        report = engine.report()
        assert [p.key for p in report.paragraphs] == [(1, 0), (2, 0)]
        assert report.paragraphs[0].actual_ms == 12_000

    def test_a_report_taken_mid_session_includes_the_open_paragraph(self):
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(7.0)

        live = engine.report()
        assert live.paragraphs_analysed == 1
        assert live.paragraphs[0].actual_ms == 7_000

    def test_finishing_twice_does_not_double_count_the_last_paragraph(self):
        engine, clock = self.build([words(40)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.session_finished()
        clock.advance(300.0)
        engine.session_finished()

        assert engine.report().paragraphs[0].actual_ms == 12_000

    def test_events_after_the_session_finished_are_ignored(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.session_finished()

        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        assert engine.report().paragraphs_analysed == 1

    def test_starting_a_second_session_clears_the_first(self):
        engine, clock = self.build([words(40), words(40, start=100)])

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        engine.session_finished()

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(6.0)
        report = engine.report()

        assert report.paragraphs_analysed == 1
        assert report.paragraphs[0].actual_ms == 6_000


class TestTheEventSeam:
    def test_the_dispatcher_covers_every_event_the_engine_acts_on(self):
        clock = VirtualClock()
        engine = FocusAnalyticsEngine(
            "session", "reader", memory=memory_with([words(40)]), baseline=MEASURED,
            clock=clock.now,
        )
        pointer = ReadingPointer(page_index=1, paragraph_index=0)

        engine.handle(SessionEvent.SESSION_STARTED, pointer)
        clock.advance(6.0)
        engine.handle(SessionEvent.MEANING_REQUESTED)
        engine.handle(SessionEvent.LOOKUP_COMPLETED)
        engine.handle(SessionEvent.MEANING_MODE_OFF)
        clock.advance(6.0)
        engine.handle(SessionEvent.SESSION_FINISHED)

        only = engine.report().paragraphs[0]
        assert only.actual_ms == 12_000
        assert (only.meaning_requests, only.lookups) == (1, 1)

    def test_an_irrelevant_event_is_ignored_rather_than_raising(self):
        # This engine observes a stream it does not own. A new event elsewhere in
        # the system must never be able to end a session by arriving here.
        engine = FocusAnalyticsEngine("session", "reader", baseline=MEASURED)

        for event in SessionEvent:
            engine.handle(event)

    def test_a_pointer_event_with_no_pointer_is_ignored(self):
        clock = VirtualClock()
        engine = FocusAnalyticsEngine(
            "session", "reader", memory=memory_with([words(40)]), baseline=MEASURED,
            clock=clock.now,
        )
        engine.handle(SessionEvent.SESSION_STARTED, ReadingPointer(page_index=1))
        engine.handle(SessionEvent.READING_POINTER_UPDATED, None)

        assert engine.report().paragraphs_analysed == 1


class TestTheReport:
    def build_session(self) -> FocusAnalyticsEngine:
        """A session with one hard paragraph, one easy one, and one interruption."""

        clock = VirtualClock()
        engine = FocusAnalyticsEngine(
            "session",
            "reader",
            memory=memory_with([words(40), words(40, start=100), words(40, start=200)]),
            baseline=MEASURED,
            clock=clock.now,
        )

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        # Paragraph 0: hard — slow, with lookups.
        clock.advance(36.0)
        engine.meaning_requested()
        engine.meaning_mode_off()
        engine.meaning_requested()
        engine.meaning_mode_off()
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        # Paragraph 1: on pace, nothing notable.
        clock.advance(12.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=2))
        # Paragraph 2: very slow, no friction at all — an interruption.
        clock.advance(120.0)
        engine.session_finished()
        return engine

    def test_the_hardest_paragraph_ranks_first(self):
        report = self.build_session().report()

        assert report.ranked[0].key == (1, 0)
        assert report.ranked[0].difficulty is DifficultyLevel.HIGH

    def test_the_interrupted_paragraph_is_excluded_from_the_ranking(self):
        # It is the slowest paragraph in the session by a wide margin. It is also
        # the one the engine knows least about, and ranking it would tell the
        # reader to revise the paragraph they were interrupted on.
        report = self.build_session().report()

        assert (1, 2) not in [p.key for p in report.ranked]
        interrupted = next(p for p in report.paragraphs if p.key == (1, 2))
        assert interrupted.difficulty is DifficultyLevel.UNKNOWN

    def test_an_easy_session_recommends_nothing(self):
        clock = VirtualClock()
        engine = FocusAnalyticsEngine(
            "session", "reader", memory=memory_with([words(40), words(40, start=100)]),
            baseline=MEASURED, clock=clock.now,
        )
        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(12.0)
        engine.pointer_updated(ReadingPointer(page_index=1, paragraph_index=1))
        clock.advance(12.0)
        engine.session_finished()

        # Padding this to a fixed length would manufacture difficulty on every
        # session that went fine.
        assert engine.report().needs_attention() == ()

    def test_needs_attention_honours_its_limit(self):
        report = self.build_session().report()

        assert len(report.needs_attention(limit=1)) <= 1

    def test_a_report_can_be_recomputed_against_a_later_baseline(self):
        engine = self.build_session()
        faster = ReadingBaseline(
            reader_id="reader", baseline_wpm=400.0, calibrated=True,
            method=CalibrationMethod.MEASURED, sample_count=5,
        )

        original = engine.report()
        rejudged = engine.report(faster)

        assert rejudged.baseline_wpm == 400.0
        assert rejudged.paragraphs[0].expected_ms < original.paragraphs[0].expected_ms
        # And asking for a report never moved the engine's own baseline: that is
        # calibration's decision, not a side effect of reading analytics.
        assert engine.baseline.baseline_wpm == 200.0
        assert engine.report().baseline_wpm == 200.0

    def test_the_totals_agree_with_the_paragraphs(self):
        report = self.build_session().report()

        assert report.total_words == sum(p.words for p in report.paragraphs)
        assert report.total_idle_ms == sum(p.idle_ms for p in report.paragraphs)
        assert report.total_focused_ms == sum(p.focused_ms for p in report.paragraphs)


class TestItRunsWithoutMergeMemory:
    def test_no_memory_means_unknown_rather_than_a_guess(self):
        # The engine cannot invent word counts, and a paragraph with no word count
        # has no expected time to compare against.
        clock = VirtualClock()
        engine = FocusAnalyticsEngine("session", "reader", baseline=MEASURED, clock=clock.now)

        engine.session_started(ReadingPointer(page_index=1, paragraph_index=0))
        clock.advance(60.0)
        engine.session_finished()

        only = engine.report().paragraphs[0]
        assert only.words == 0
        assert only.difficulty is DifficultyLevel.UNKNOWN

    def test_a_real_merge_memory_satisfies_the_contract_the_engine_asks_for(self):
        assert isinstance(memory_with([words(40)]), MergeMemorySource)
