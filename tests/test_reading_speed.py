"""Reading Speed tests.

Every test drives an injected clock instead of sleeping, so the whole suite runs
in milliseconds and a "40 second" reading interval is exact rather than
approximate. No network, no audio, no database.

The tests are organised around the module's three prohibitions — it never owns
the session, the pointer, or TTS — plus the two mistakes the design exists to
avoid: a baseline that drifts during reading, and difficulty inferred from time
alone.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.modules.audio_engine.models import PlaybackStatistics, ReadingPointer
from backend.app.modules.reading_speed import calibration, interfaces, predictor
from backend.app.modules.reading_speed.analytics import (
    MIN_MEASURABLE_READING_MS,
    PageObservation,
    assess_page,
    summarize_session,
)
from backend.app.modules.reading_speed.calibration import CalibrationError
from backend.app.modules.reading_speed.models import (
    DEFAULT_BASELINE_WPM,
    CalibrationMethod,
    ContentMap,
    DifficultyLevel,
    ProgressSnapshot,
    ReadingBaseline,
    ReadingMode,
    SentenceSpan,
)
from backend.app.modules.reading_speed.placeholders import (
    ProgressTrackerPlaceholder,
    ReadingSpeedServicePlaceholder,
)
from backend.app.modules.reading_speed.service import (
    ReadingSpeedService,
    UnknownSessionError,
)
from backend.app.modules.reading_speed.tracker import ProgressTracker


class Clock:
    """A hand-cranked monotonic clock.

    Tests advance time explicitly so a 40-second reading interval is exactly
    40000ms. Sleeping would make the same assertions flaky and the suite slow.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_content(pages: int = 2, sentences: int = 10, words: int = 20) -> ContentMap:
    """A content map with uniform pages, so expected times are easy to reason about."""

    spans = tuple(
        SentenceSpan(
            pointer=ReadingPointer(page_index=p, paragraph_index=0, sentence_index=s),
            word_count=words,
        )
        for p in range(1, pages + 1)
        for s in range(sentences)
    )
    return ContentMap(
        sentences=spans,
        page_word_counts={p: sentences * words for p in range(1, pages + 1)},
        source_version=1,
    )


def measured(wpm: float = 200.0, reader_id: str = "reader") -> ReadingBaseline:
    return ReadingBaseline(
        reader_id=reader_id,
        baseline_wpm=wpm,
        calibrated=True,
        method=CalibrationMethod.MEASURED,
        sample_count=1,
    )


class TestCalibration:
    def test_measures_wpm_from_a_timed_passage(self):
        baseline = calibration.calibrate_from_passage(
            "reader", word_count=180, elapsed_ms=60_000
        )
        assert baseline.baseline_wpm == 180.0
        assert baseline.method is CalibrationMethod.MEASURED
        assert baseline.is_evidence

    def test_default_baseline_is_marked_as_a_guess(self):
        baseline = calibration.default_baseline("reader")
        assert baseline.baseline_wpm == DEFAULT_BASELINE_WPM
        assert not baseline.calibrated
        assert not baseline.is_evidence

    def test_manual_baseline_is_a_claim_not_a_measurement(self):
        baseline = calibration.calibrate_manually("reader", baseline_wpm=400)
        assert baseline.calibrated
        assert baseline.method is CalibrationMethod.MANUAL
        # Deliberately not evidence: readers overestimate their own pace.
        assert not baseline.is_evidence

    @pytest.mark.parametrize(
        "word_count, elapsed_ms",
        [
            (10, 60_000),  # passage too short to separate reading from reaction
            (180, 5_000),  # timer too short
            (180, 1_000_000),  # 11 wpm — the reader stopped, the timer did not
        ],
    )
    def test_implausible_calibration_raises_rather_than_clamping(self, word_count, elapsed_ms):
        # Clamping would turn a detectable mistake into a permanent wrong baseline.
        with pytest.raises(CalibrationError):
            calibration.calibrate_from_passage(
                "reader", word_count=word_count, elapsed_ms=elapsed_ms
            )


class TestBaselineStability:
    """The mistake this module exists to avoid: a baseline that chases itself."""

    def test_adapt_weights_the_old_baseline_at_eighty_percent(self):
        adapted = calibration.adapt(
            measured(200.0), session_wpm=100.0, reading_ms=300_000, words_read=500
        )
        # 0.8*200 + 0.2*100. A slow session nudges the baseline; it does not redefine it.
        assert adapted.baseline_wpm == 180.0
        assert adapted.method is CalibrationMethod.ADAPTED
        assert adapted.sample_count == 2

    @pytest.mark.parametrize(
        "reading_ms, words_read",
        [
            (10_000, 500),  # read for ten seconds
            (300_000, 40),  # forty words in five minutes
        ],
    )
    def test_thin_evidence_leaves_the_baseline_untouched(self, reading_ms, words_read):
        original = measured(200.0)
        assert (
            calibration.adapt(
                original, session_wpm=90.0, reading_ms=reading_ms, words_read=words_read
            )
            is original
        )

    def test_implausible_session_pace_leaves_the_baseline_untouched(self):
        original = measured(200.0)
        assert (
            calibration.adapt(
                original, session_wpm=5_000.0, reading_ms=300_000, words_read=500
            )
            is original
        )

    def test_first_real_session_replaces_a_guess_instead_of_averaging_with_it(self):
        guess = calibration.default_baseline("reader")
        adapted = calibration.adapt(
            guess, session_wpm=120.0, reading_ms=300_000, words_read=500
        )
        # Blending would dilute a measurement with a number we invented.
        assert adapted.baseline_wpm == 120.0
        assert adapted.is_evidence

    def test_repeated_sessions_converge_instead_of_drifting(self):
        """The long-session property: many adaptations settle, they do not wander.

        Single-step weighting is checked above, which says nothing about what
        forty sessions do. An EWMA weight above 1.0 — or a sign slip — still
        passes the one-step test while diverging here, and the symptom would be
        a reader whose baseline slowly walks away from their actual pace.
        """

        baseline = measured(200.0)
        history = []
        for _ in range(40):
            baseline = calibration.adapt(
                baseline, session_wpm=250.0, reading_ms=600_000, words_read=2_000
            )
            history.append(baseline.baseline_wpm)

        # Monotone towards the observed pace, never past it, and settled by the end.
        assert history == sorted(history)
        assert all(value <= 250.0 for value in history)
        assert history[-1] == pytest.approx(250.0, abs=0.5)
        assert history[-1] == history[-2], "a converged baseline must stop moving"

    def test_a_reader_with_two_paces_settles_in_between_rather_than_swinging(self):
        """Alternating fast and slow sessions must not amplify.

        The failure this rules out is a baseline that swings wider each time,
        which would make every prediction alternately far too fast and far too
        slow — worse than a baseline that is simply a bit wrong.
        """

        baseline = measured(200.0)
        history = []
        for index in range(20):
            baseline = calibration.adapt(
                baseline,
                session_wpm=300.0 if index % 2 == 0 else 150.0,
                reading_ms=600_000,
                words_read=2_000,
            )
            history.append(baseline.baseline_wpm)

        early_swing = max(history[:4]) - min(history[:4])
        late_swing = max(history[-4:]) - min(history[-4:])
        assert late_swing <= early_swing + 1.0, "the swing must not grow"
        assert 150.0 < min(history[-4:]) and max(history[-4:]) < 300.0

    def test_suggest_does_not_apply(self):
        original = measured(200.0)
        suggestion = calibration.suggest_baseline(
            original, session_wpm=100.0, reading_ms=300_000, words_read=500
        )
        assert suggestion == 180.0
        assert original.baseline_wpm == 200.0

    def test_baseline_cannot_be_mutated_in_place(self):
        # Frozen so a baseline cannot be edited mid-session by accident.
        with pytest.raises(Exception):
            measured().baseline_wpm = 500.0


class TestPrediction:
    def test_predicts_expected_position_from_baseline_and_elapsed_reading_time(self):
        content = make_content()
        snapshot = ProgressSnapshot(
            session_id="s",
            reader_id="r",
            pointer=ReadingPointer(page_index=1, sentence_index=5),
            elapsed_reading_ms=60_000,
        )
        prediction = predictor.predict(snapshot, measured(200.0), content)

        # 200 wpm for 60s = 200 words expected; the pointer sits at 100.
        assert prediction.expected_word_offset == 200
        assert prediction.actual_word_offset == 100
        assert prediction.deviation_words == -100
        assert prediction.is_behind

    def test_reader_ahead_of_baseline_reports_positive_word_deviation(self):
        content = make_content()
        snapshot = ProgressSnapshot(
            session_id="s",
            reader_id="r",
            pointer=ReadingPointer(page_index=2, sentence_index=0),
            elapsed_reading_ms=30_000,
        )
        prediction = predictor.predict(snapshot, measured(200.0), content)
        assert prediction.deviation_words > 0
        assert prediction.is_ahead
        # Ahead in words means less time taken than predicted.
        assert prediction.deviation_ms < 0

    def test_slower_reader_reports_positive_millisecond_deviation(self):
        """The two deviations point opposite ways on purpose."""

        content = make_content()
        snapshot = ProgressSnapshot(
            session_id="s",
            reader_id="r",
            pointer=ReadingPointer(page_index=1, sentence_index=5),
            elapsed_reading_ms=60_000,
        )
        prediction = predictor.predict(snapshot, measured(200.0), content)
        # 100 words should take 30s at 200 wpm; it took 60s.
        assert prediction.deviation_ms == 30_000
        assert prediction.deviation_ratio == 1.0

    def test_session_starting_mid_book_is_not_reported_as_hopelessly_behind(self):
        content = make_content(pages=4)
        pointer = ReadingPointer(page_index=3, sentence_index=0)
        snapshot = ProgressSnapshot(
            session_id="s", reader_id="r", pointer=pointer, elapsed_reading_ms=1_000
        )
        origin = content.words_before(pointer)

        naive = predictor.predict(snapshot, measured(200.0), content)
        anchored = predictor.predict(
            snapshot, measured(200.0), content, origin_word_offset=origin
        )

        # Measured from word zero, opening at page 3 credits the reader with 400
        # words they never read in this session — absurdly ahead after one second.
        assert naive.deviation_words > 300
        assert anchored.deviation_words == pytest.approx(0, abs=5)

    def test_prediction_is_pure(self):
        content = make_content()
        snapshot = ProgressSnapshot(
            session_id="s", reader_id="r", elapsed_reading_ms=45_000, words_confirmed=10
        )
        baseline = measured(200.0)
        first = predictor.predict(snapshot, baseline, content)
        second = predictor.predict(snapshot, baseline, content)
        assert first == second
        assert snapshot.elapsed_reading_ms == 45_000
        assert baseline.baseline_wpm == 200.0

    def test_confidence_is_low_early_and_when_the_baseline_is_a_guess(self):
        content = make_content()
        early = ProgressSnapshot(session_id="s", reader_id="r", elapsed_reading_ms=1_000)
        settled = ProgressSnapshot(session_id="s", reader_id="r", elapsed_reading_ms=120_000)

        assert predictor.predict(early, measured(), content).confidence < 0.2
        assert predictor.predict(settled, measured(), content).confidence == 1.0

        guessed = predictor.predict(settled, calibration.default_baseline("r"), content)
        assert guessed.confidence < 0.5
        # Still answers — a UI needs something to show from the first second.
        assert guessed.baseline_wpm == DEFAULT_BASELINE_WPM

    def test_empty_content_map_yields_zero_confidence(self):
        snapshot = ProgressSnapshot(session_id="s", reader_id="r", elapsed_reading_ms=60_000)
        assert predictor.predict(snapshot, measured(), ContentMap()).confidence == 0.0

    def test_lagging_content_map_does_not_cap_progress(self):
        """A gesture jump onto a page OCR has not merged yet must not stall progress.

        The map ends at page 1, so it reports the same total for every pointer
        beyond it. Without the tracker's tally the reader would look stopped.
        """

        content = make_content(pages=1)
        snapshot = ProgressSnapshot(
            session_id="s",
            reader_id="r",
            pointer=ReadingPointer(page_index=9, sentence_index=0),
            elapsed_reading_ms=60_000,
            words_confirmed=350,
        )
        assert predictor.predict(snapshot, measured(), content).actual_word_offset == 350

    def test_observed_wpm_declines_to_answer_before_there_is_evidence(self):
        content = make_content()
        snapshot = ProgressSnapshot(session_id="s", reader_id="r", elapsed_reading_ms=0)
        assert predictor.observed_wpm(snapshot, content) == 0.0


class TestTracker:
    def test_reading_clock_excludes_paused_time_and_wall_clock_does_not(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()

        clock.advance(30)
        tracker.session_paused()
        clock.advance(120)  # the reader answered the door
        tracker.session_resumed()
        clock.advance(30)

        snapshot = tracker.snapshot()
        assert snapshot.elapsed_reading_ms == 60_000
        assert snapshot.elapsed_wall_ms == 180_000

    def test_meaning_mode_freezes_the_reading_clock_and_is_counted_separately(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()

        clock.advance(20)
        tracker.meaning_mode_on()
        clock.advance(45)
        tracker.lookup_completed()
        tracker.meaning_mode_off()
        clock.advance(10)

        snapshot = tracker.snapshot()
        # Looking up a word must never make the reader appear slower.
        assert snapshot.elapsed_reading_ms == 30_000
        assert snapshot.meaning_mode_count == 1
        assert snapshot.lookup_count == 1
        assert snapshot.mode is ReadingMode.READING

    def test_pointer_update_infers_a_page_change(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()
        clock.advance(60)

        tracker.pointer_updated(ReadingPointer(page_index=2, sentence_index=0))

        assert [o.page_index for o in tracker.observations] == [1]
        assert tracker.snapshot().pages_visited == 2

    def test_corrections_and_revisits_are_recorded_as_friction(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()
        clock.advance(30)

        tracker.pointer_updated(ReadingPointer(page_index=1, sentence_index=2), corrected=True)
        clock.advance(30)
        tracker.pointer_updated(ReadingPointer(page_index=2, sentence_index=0))
        clock.advance(30)
        tracker.pointer_updated(ReadingPointer(page_index=1, sentence_index=5))

        snapshot = tracker.snapshot()
        assert snapshot.pointer_corrections == 1
        assert snapshot.page_revisits == 1

    def test_forward_page_turn_credits_the_whole_page_despite_a_lagging_pointer(self):
        """Gesture reports a position when the reader moves it, not once per sentence."""

        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()

        clock.advance(20)
        tracker.pointer_updated(ReadingPointer(page_index=1, sentence_index=3))
        clock.advance(40)
        tracker.pointer_updated(ReadingPointer(page_index=2, sentence_index=0))

        page_one = tracker.observations[0]
        # Not 80. Crediting only the words up to the pointer against the full page
        # time would rate every ordinary page as difficult.
        assert page_one.words == 200

    def test_session_end_credits_only_what_the_pointer_reached(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()
        clock.advance(30)
        tracker.pointer_updated(ReadingPointer(page_index=1, sentence_index=4))
        tracker.session_finished()

        # The reader stopped here; they did not read the rest of the page.
        assert tracker.observations[0].words == 80

    def test_session_finished_is_idempotent(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()
        clock.advance(60)
        tracker.session_finished()

        first = tracker.snapshot()
        clock.advance(300)
        tracker.session_finished()

        assert tracker.snapshot().elapsed_reading_ms == first.elapsed_reading_ms
        assert len(tracker.observations) == 1

    def test_clocks_stop_after_the_session_finishes(self):
        clock = Clock()
        tracker = ProgressTracker("s", "r", content=make_content(), clock=clock)
        tracker.session_started()
        clock.advance(60)
        tracker.session_finished()
        clock.advance(600)

        snapshot = tracker.snapshot()
        assert snapshot.elapsed_reading_ms == 60_000
        assert snapshot.elapsed_wall_ms == 60_000

    def test_stale_content_map_is_rejected(self):
        tracker = ProgressTracker("s", "r", content=make_content(), clock=Clock())
        tracker.session_started()

        newer = make_content().model_copy(update={"source_version": 5})
        assert tracker.set_content(newer) is True

        stale = make_content().model_copy(update={"source_version": 2})
        assert tracker.set_content(stale) is False
        assert tracker.content.source_version == 5

    def test_snapshot_carries_no_pace(self):
        """Progress is observed; pace is established. Two sources would diverge."""

        assert "wpm" not in ProgressSnapshot.model_fields
        assert not any("wpm" in name for name in ProgressSnapshot.model_fields)


class TestDifficulty:
    def test_slow_page_with_no_friction_is_unknown_not_hard(self):
        """A reader who set the book down looks identical to one who struggled."""

        observation = PageObservation(page_index=1, words=200, reading_ms=240_000)
        assessed = assess_page(observation, measured(200.0))
        assert assessed.difficulty is DifficultyLevel.UNKNOWN
        assert any("interruption" in reason for reason in assessed.evidence)

    def test_slow_page_with_corroborating_friction_is_hard(self):
        observation = PageObservation(
            page_index=1,
            words=200,
            reading_ms=240_000,
            lookups=3,
            meaning_requests=2,
        )
        assessed = assess_page(observation, measured(200.0))
        assert assessed.difficulty is DifficultyLevel.HIGH
        assert assessed.evidence  # the verdict always ships with its reasons

    def test_on_pace_page_is_low(self):
        observation = PageObservation(page_index=1, words=200, reading_ms=60_000)
        assert assess_page(observation, measured(200.0)).difficulty is DifficultyLevel.LOW

    def test_difficulty_is_not_inferred_from_a_guessed_baseline(self):
        observation = PageObservation(
            page_index=1, words=200, reading_ms=240_000, lookups=5
        )
        assessed = assess_page(observation, calibration.default_baseline("r"))
        assert assessed.difficulty is DifficultyLevel.UNKNOWN

    def test_short_page_is_not_rated(self):
        observation = PageObservation(page_index=1, words=12, reading_ms=90_000)
        assert assess_page(observation, measured()).difficulty is DifficultyLevel.UNKNOWN

    @pytest.mark.parametrize(
        "reading_ms", [0, 15, MIN_MEASURABLE_READING_MS - 1]
    )
    def test_page_crossed_too_fast_to_measure_is_unknown_not_easy(self, reading_ms):
        """A page nobody spent time on yields no verdict, not a flattering one.

        Caught in the simulator, not here: a -100% deviation falls through every
        slow branch and lands on the on-pace default, so the page came back LOW
        "within 100% of the predicted time" — a sentence that reads like a finding
        but is really the absence of one.
        """

        observation = PageObservation(page_index=1, words=200, reading_ms=reading_ms)
        assessed = assess_page(observation, measured(200.0))
        assert assessed.difficulty is DifficultyLevel.UNKNOWN
        assert any("skipped, not read" in reason for reason in assessed.evidence)

    def test_a_page_read_just_over_the_floor_is_still_rated(self):
        """The floor rejects unmeasurable pages, not merely fast ones.

        Pinned to the constant rather than to 1000 so that moving the floor moves
        the boundary case with it — a literal here would keep passing while
        testing an interval that is no longer the edge.
        """

        observation = PageObservation(
            page_index=1, words=200, reading_ms=MIN_MEASURABLE_READING_MS
        )
        assert assess_page(observation, measured(200.0)).difficulty is DifficultyLevel.LOW

    def test_a_genuinely_fast_page_is_still_rated_low(self):
        """The skip guard must not swallow ordinary fast reading."""

        observation = PageObservation(page_index=1, words=200, reading_ms=45_000)
        assert assess_page(observation, measured(200.0)).difficulty is DifficultyLevel.LOW

    def test_deviation_ratio_is_comparable_across_page_lengths(self):
        short = assess_page(
            PageObservation(page_index=1, words=50, reading_ms=22_500), measured(200.0)
        )
        long = assess_page(
            PageObservation(page_index=2, words=500, reading_ms=225_000), measured(200.0)
        )
        assert short.deviation_ratio == long.deviation_ratio == 0.5


class TestSessionAnalytics:
    def _snapshot(self, **overrides) -> ProgressSnapshot:
        base = dict(
            session_id="s",
            reader_id="r",
            elapsed_reading_ms=300_000,
            elapsed_wall_ms=400_000,
            words_confirmed=1_000,
            lookup_count=2,
            meaning_mode_count=1,
        )
        return ProgressSnapshot(**{**base, **overrides})

    def test_ingests_playback_statistics_when_tts_was_on(self):
        playback = PlaybackStatistics(
            words_spoken=900, reading_time_ms=270_000, playback_time_ms=350_000, pages_read=3
        )
        summary = summarize_session(
            self._snapshot(), measured(200.0), observations=[], playback=playback
        )
        # The audio engine counted words while speaking them; that beats inference.
        assert summary.words_read == 900
        assert summary.reading_duration_ms == 270_000
        assert summary.tts_assisted

    def test_silent_reading_uses_the_tracker(self):
        summary = summarize_session(self._snapshot(), measured(200.0), observations=[])
        assert summary.words_read == 1_000
        assert not summary.tts_assisted
        assert summary.session_wpm == 200.0

    def test_identifies_hardest_and_easiest_pages(self):
        observations = [
            PageObservation(page_index=1, words=200, reading_ms=60_000),
            PageObservation(page_index=2, words=200, reading_ms=180_000, lookups=4),
            PageObservation(page_index=3, words=200, reading_ms=45_000),
        ]
        summary = summarize_session(
            self._snapshot(), measured(200.0), observations=observations
        )
        assert summary.hardest_page == 2
        assert summary.easiest_page == 3

    def test_pace_is_withheld_when_the_clock_barely_ran(self):
        """A pace divided out of milliseconds is arithmetic, not a measurement.

        The simulator produced "session pace 348000 wpm" from 87 words and 15ms.
        `calibration` already refuses implausible baselines, but it was rejecting
        this number after the fact rather than never being handed it — and the
        figure still reached the summary the UI renders.
        """

        summary = summarize_session(
            self._snapshot(elapsed_reading_ms=15, elapsed_wall_ms=15, words_confirmed=87),
            measured(200.0),
            observations=[],
        )
        assert summary.session_wpm == 0.0
        assert summary.suggested_baseline_wpm is None

    def test_summarizing_never_moves_the_baseline(self):
        service = ReadingSpeedService(clock=Clock())
        service.calibrate("r", word_count=200, elapsed_ms=60_000)
        before = service.baseline_for("r")

        summary = summarize_session(
            self._snapshot(reader_id="r"), before, observations=[]
        )
        assert service.baseline_for("r") == before
        assert summary.suggested_baseline_wpm is not None


class TestService:
    def _service(self, clock: Clock) -> ReadingSpeedService:
        service = ReadingSpeedService(clock=clock)
        service.calibrate("reader", word_count=200, elapsed_ms=60_000)
        return service

    def test_sessions_are_isolated(self):
        """One reader's page turn must not close another reader's page."""

        clock = Clock()
        service = self._service(clock)
        content = make_content()
        service.start_session(session_id="a", reader_id="reader", content=content)
        service.start_session(session_id="b", reader_id="reader", content=content)

        clock.advance(60)
        service.update_pointer("a", ReadingPointer(page_index=2, sentence_index=0))
        clock.advance(30)

        assert service.tracker("a").snapshot().pages_visited == 2
        assert service.tracker("b").snapshot().pages_visited == 1
        assert service.tracker("b").snapshot().pointer.page_index == 1

    def test_unknown_session_raises_rather_than_being_invented(self):
        # An invented tracker would start its clock now and report a plausible
        # pace for a session that never began.
        with pytest.raises(UnknownSessionError):
            self._service(Clock()).predict("never-started")

    def test_reader_without_a_baseline_still_gets_predictions(self):
        service = ReadingSpeedService(clock=Clock())
        service.start_session(session_id="s", reader_id="new", content=make_content())
        prediction = service.predict("s")
        assert prediction.baseline_wpm == DEFAULT_BASELINE_WPM
        assert not service.baseline_for("new").is_evidence

    def test_finish_does_not_move_the_baseline_but_apply_does(self):
        clock = Clock()
        service = self._service(clock)
        service.start_session(session_id="s", reader_id="reader", content=make_content())

        clock.advance(120)
        service.update_pointer("s", ReadingPointer(page_index=2, sentence_index=9))
        summary = service.finish_session("s")

        assert service.baseline_for("reader").baseline_wpm == 200.0
        applied = service.apply_suggested_baseline(summary)
        assert applied.baseline_wpm != 200.0
        assert service.baseline_for("reader") == applied

    def test_reading_a_summary_twice_does_not_move_the_baseline_twice(self):
        clock = Clock()
        service = self._service(clock)
        service.start_session(session_id="s", reader_id="reader", content=make_content())
        clock.advance(120)
        service.update_pointer("s", ReadingPointer(page_index=2, sentence_index=9))

        first = service.finish_session("s")
        second = service.finish_session("s")
        assert first.session_wpm == second.session_wpm
        assert service.baseline_for("reader").baseline_wpm == 200.0

    def test_close_discards_the_tracker(self):
        service = self._service(Clock())
        service.start_session(session_id="s", reader_id="reader")
        assert service.close("s") is True
        assert service.close("s") is False
        assert not service.has("s")


class TestNeverDrivesTheSystem:
    """The three prohibitions, made executable.

    A future change that adds playback control or a pointer setter to this module
    fails here rather than in review.
    """

    @pytest.mark.parametrize("forbidden", ["start", "pause", "resume", "stop", "seek", "speak"])
    def test_service_exposes_no_playback_control(self, forbidden):
        # `pause`/`resume` here record that a session was paused; they must never
        # be playback controls. Guard the TTS-shaped names the audio engine owns.
        assert not hasattr(ReadingSpeedService, f"{forbidden}_playback")
        assert not hasattr(ReadingSpeedService, "synthesize")

    @pytest.mark.parametrize(
        "forbidden", ["set_pointer", "advance", "seek", "set_pace", "set_wpm"]
    )
    def test_nothing_can_move_the_reader(self, forbidden):
        assert not hasattr(ProgressTracker, forbidden)
        assert not hasattr(ReadingSpeedService, forbidden)

    def test_content_map_is_read_only_from_merge_memory(self):
        tracker = ProgressTracker("s", "r", content=make_content(), clock=Clock())
        tracker.session_started()
        original = tracker.content

        tracker.pointer_updated(ReadingPointer(page_index=2, sentence_index=3))
        tracker.lookup_completed()
        tracker.session_finished()

        assert tracker.content is original


class TestInterfaceConformance:
    """Protocols are documentation; these make them enforceable.

    The audio engine's interfaces silently drifted from its implementation, and
    only an executable check found it.
    """

    @staticmethod
    def _methods(protocol) -> list[str]:
        return [
            name
            for name, member in vars(protocol).items()
            if not name.startswith("_") and (callable(member) or isinstance(member, property))
        ]

    @pytest.mark.parametrize(
        "protocol, implementation",
        [
            (interfaces.ProgressTrackerInterface, ProgressTracker),
            (interfaces.ProgressTrackerInterface, ProgressTrackerPlaceholder),
            (interfaces.ReadingSpeedServiceInterface, ReadingSpeedService),
            (interfaces.ReadingSpeedServiceInterface, ReadingSpeedServicePlaceholder),
        ],
    )
    def test_implementation_satisfies_protocol(self, protocol, implementation):
        missing = [name for name in self._methods(protocol) if not hasattr(implementation, name)]
        assert not missing, f"{implementation.__name__} is missing {missing}"

    @pytest.mark.parametrize(
        "protocol, implementation",
        [
            (interfaces.ProgressTrackerInterface, ProgressTracker),
            (interfaces.ProgressTrackerInterface, ProgressTrackerPlaceholder),
            (interfaces.ReadingSpeedServiceInterface, ReadingSpeedService),
            (interfaces.ReadingSpeedServiceInterface, ReadingSpeedServicePlaceholder),
        ],
    )
    def test_signatures_match_protocol(self, protocol, implementation):
        """Parameter names and defaults only.

        Return annotations are skipped deliberately: returning a concrete type
        where the Protocol declares an interface is valid covariance, not drift.
        """

        for name in self._methods(protocol):
            expected = inspect.signature(getattr(protocol, name))
            actual = inspect.signature(getattr(implementation, name))
            assert [p.name for p in expected.parameters.values()] == [
                p.name for p in actual.parameters.values()
            ], f"{implementation.__name__}.{name} parameters differ"
            assert [p.default for p in expected.parameters.values()] == [
                p.default for p in actual.parameters.values()
            ], f"{implementation.__name__}.{name} defaults differ"


class TestRoutes:
    """Transport only. The service is exercised directly everywhere else."""

    BASE = "/reading-speed"

    @pytest.fixture
    def client(self):
        from backend.app.modules.reading_speed.service import reading_speed_service

        reading_speed_service.close_all()
        with TestClient(app) as client:
            yield client
        reading_speed_service.close_all()

    def test_calibrate_then_predict(self, client):
        assert client.post(
            f"{self.BASE}/calibrate",
            json={"reader_id": "r", "word_count": 200, "elapsed_ms": 60_000},
        ).json()["data"]["baseline"]["baseline_wpm"] == 200.0

        client.post(
            f"{self.BASE}/sessions/s/start",
            json={"reader_id": "r", "pointer": {"page_index": 1}},
        )
        body = client.get(f"{self.BASE}/sessions/s/prediction").json()
        assert body["success"]
        assert "prediction" in body["data"]

    def test_implausible_calibration_is_rejected(self, client):
        response = client.post(
            f"{self.BASE}/calibrate",
            json={"reader_id": "r", "word_count": 200, "elapsed_ms": 2_000},
        )
        assert response.status_code == 422

    def test_unknown_session_is_not_invented(self, client):
        assert client.get(f"{self.BASE}/sessions/ghost/prediction").status_code == 404
        assert client.post(f"{self.BASE}/sessions/ghost/lookup").status_code == 404

    def test_default_baseline_is_served_and_flagged(self, client):
        body = client.get(f"{self.BASE}/baseline/stranger").json()["data"]
        assert body["baseline"]["baseline_wpm"] == DEFAULT_BASELINE_WPM
        assert body["is_evidence"] is False

    def test_stale_content_is_rejected_without_an_error(self, client):
        client.post(f"{self.BASE}/sessions/s/start", json={"reader_id": "r"})
        client.post(
            f"{self.BASE}/sessions/s/content",
            json={"content": {"sentences": [], "page_word_counts": {}, "source_version": 4}},
        )
        response = client.post(
            f"{self.BASE}/sessions/s/content",
            json={"content": {"sentences": [], "page_word_counts": {}, "source_version": 1}},
        )
        assert response.status_code == 200
        assert response.json()["data"]["applied"] is False
        assert response.json()["data"]["source_version"] == 4
