"""Gesture Engine tests.

Ported from `backend/app/OCRandGESTURE/Gesture/tests/` — `test_selector.py`,
`test_models.py`, `test_finger_detector.py`, `test_integration.py` and
`test_regressions.py` — against the migrated module rather than the standalone
package. The assertions are the standalone's, because they encode geometry that
was tuned against real hands on real pages; what changed is the imports, the word
type (`RecognizedWord`), and the pipeline entry point.

The regression cases are the valuable part of this file. Each one pins a defect
that crashed the running controller: an empty-string word from Google Vision, a
bbox built as (x, y, w, h) instead of (x_min, y_min, x_max, y_max), raw dicts
passed where words were expected. Those bugs are all reachable through the
migrated code too, so the tests come with it.

No network, no camera. Frames are drawn with OpenCV and gestures are either a
synthetic skin blob or an injected `FingerPoint`.
"""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from backend.app.modules.gesture_engine import (
    API_VERSION,
    GesturePipeline,
    draw_overlay,
    select_word,
)
from backend.app.modules.gesture_engine.detector import (
    detect_finger,
    detect_finger_contour_fallback,
    estimate_direction,
)
from backend.app.modules.gesture_engine.selection_models import (
    CoordinateTransformer,
    FingerPoint,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
    SelectionStrategy,
)
from backend.app.modules.gesture_engine.selector import (
    get_search_polygon,
    group_ocr_words,
    select_intended_word,
)
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.providers import GoogleVisionProvider
from backend.app.shared.events import SessionEvent


def word(text, bbox, **kwargs):
    """A `RecognizedWord` with its centre derived, as a provider would build it."""

    return RecognizedWord.from_bbox(text, bbox, **kwargs)


def skin_bgr():
    """A BGR value inside the detector's HSV skin range."""

    return tuple(
        int(channel)
        for channel in cv2.cvtColor(
            np.array([[[10, 150, 200]]], dtype=np.uint8), cv2.COLOR_HSV2BGR
        )[0, 0]
    )


def blank_page(width=400, height=300):
    return np.full((height, width, 3), 255, dtype=np.uint8)


def page_with_finger(tip_x=160, tip_y=80, width=400, height=300):
    """A white page with a skin-coloured finger reaching up to (tip_x, tip_y)."""

    image = blank_page(width, height)
    cv2.rectangle(image, (tip_x - 10, tip_y), (tip_x + 10, tip_y + 100), skin_bgr(), -1)
    return image


@pytest.fixture
def two_lines():
    """Two lines of six words, the layout the standalone integration test used."""

    return [
        word("Read", (50, 40, 110, 70), line_index=0, paragraph_index=0, word_index=0),
        word("books", (120, 40, 200, 70), line_index=0, paragraph_index=0, word_index=1),
        word("daily.", (210, 40, 290, 70), line_index=0, paragraph_index=0, word_index=2),
        word("Learn", (50, 100, 120, 130), line_index=1, paragraph_index=0, word_index=3),
        word("something", (130, 100, 260, 130), line_index=1, paragraph_index=0, word_index=4),
        word("new.", (270, 100, 330, 130), line_index=1, paragraph_index=0, word_index=5),
    ]


class TestModels:
    def test_enum_values_are_stable(self):
        # These strings cross the API boundary; renaming one silently breaks the
        # frontend's status handling.
        assert SelectionStatus.SUCCESS.value == "success"
        assert SelectionStrategy.AUTO.value == "auto"

    def test_finger_point_defaults_to_mediapipe(self):
        point = FingerPoint(x=100.0, y=150.0, confidence=0.9, direction=(0.0, -1.0))
        assert (point.x, point.y) == (100.0, 150.0)
        assert point.detection_method == "mediapipe"

    def test_recognized_word_derives_its_centre(self):
        recognised = word("test", (10, 20, 50, 40))
        assert recognised.bbox == (10, 20, 50, 40)
        assert recognised.center_x == pytest.approx(30.0)
        assert recognised.center_y == pytest.approx(30.0)
        # -1, not 0: the provider did not say which word this was.
        assert recognised.word_index == -1

    def test_coordinate_transformer_round_trips(self):
        transformer = CoordinateTransformer(
            scale_x=2.0, scale_y=0.5, offset_x=10.0, offset_y=-5.0
        )
        assert transformer.transform(10.0, 100.0) == (30.0, 45.0)
        assert transformer.inverse_transform(30.0, 45.0) == (10.0, 100.0)


class TestLineClustering:
    def test_words_cluster_into_lines_by_vertical_position(self):
        words = [
            word("Hello", (10, 20, 60, 40)),
            word("World", (80, 20, 130, 40)),
            word("Python", (10, 70, 70, 90)),
            word("Rules", (90, 70, 140, 90)),
        ]
        lines = group_ocr_words(words, SelectionConfig(line_cluster_tolerance=0.5))

        assert len(lines) == 2
        assert [w.text for w in lines[0].words] == ["Hello", "World"]
        assert [w.text for w in lines[1].words] == ["Python", "Rules"]
        # The cluster assigns the index; the provider's own line_index was -1 here.
        assert all(w.line_index == 0 for w in lines[0].words)
        assert all(w.line_index == 1 for w in lines[1].words)

    def test_words_are_sorted_left_to_right_within_a_line(self):
        # Fed in reverse order to prove clustering sorts rather than preserves.
        lines = group_ocr_words(
            [word("second", (80, 20, 130, 40)), word("first", (10, 20, 60, 40))],
            SelectionConfig(),
        )
        assert [w.text for w in lines[0].words] == ["first", "second"]


class TestSelection:
    def test_finger_below_a_word_selects_it(self, two_lines):
        finger = FingerPoint(x=160.0, y=85.0, confidence=0.9, direction=(0.0, -1.0))
        result = select_intended_word(finger, two_lines, SelectionConfig())

        assert result.status is SelectionStatus.SUCCESS
        assert result.selected_word == "books"
        assert result.selected_line == "Read books daily."
        assert result.line_index == 0

    def test_direct_touch_wins_over_the_pointing_cone(self, two_lines):
        # Inside "something"'s box while pointing up at line 0. Touch is checked
        # first because a reader resting on a word means that word.
        finger = FingerPoint(x=195.0, y=115.0, confidence=0.9, direction=(0.0, -1.0))
        result = select_intended_word(finger, two_lines, SelectionConfig())
        assert result.selected_word == "something"

    def test_empty_ocr_reports_its_own_status(self):
        finger = FingerPoint(x=100.0, y=100.0, confidence=0.9, direction=(0.0, -1.0))
        result = select_intended_word(finger, [], SelectionConfig())
        # Not NO_WORD_FOUND: there was nothing to search, which is a different
        # fact from having searched and failed.
        assert result.status is SelectionStatus.OCR_EMPTY

    def test_finger_far_from_all_text_finds_nothing(self, two_lines):
        finger = FingerPoint(x=380.0, y=290.0, confidence=0.9, direction=(0.0, 1.0))
        result = select_intended_word(finger, two_lines, SelectionConfig())
        assert result.status in (SelectionStatus.NO_WORD_FOUND, SelectionStatus.LOW_CONFIDENCE)
        assert result.selected_word == ""

    def test_search_polygon_widens_when_pointing_confidence_is_low(self):
        confident = FingerPoint(x=100.0, y=100.0, confidence=0.95, direction=(0.0, -1.0))
        unsure = FingerPoint(x=100.0, y=100.0, confidence=0.15, direction=(0.0, -1.0))
        config = SelectionConfig()

        def span(polygon):
            xs = [x for x, _ in polygon]
            return max(xs) - min(xs)

        # A low-confidence fingertip has an unreliable direction, so the cone
        # widens rather than committing to a heading it does not trust.
        assert span(get_search_polygon(unsure, 30.0, 60.0, config)) > span(
            get_search_polygon(confident, 30.0, 60.0, config)
        )

    def test_candidate_scores_are_ranked_best_first(self, two_lines):
        # Below line 1 by more than half a line height, so the direct-touch check
        # misses and the cone runs. The gap between the two lines is exactly the
        # touch threshold, so pointing from underneath is the only way to reach
        # stage 2 with this layout.
        finger = FingerPoint(x=195.0, y=150.0, confidence=0.9, direction=(0.0, -1.0))
        result = select_intended_word(finger, two_lines, SelectionConfig())

        scores = [candidate.total_score for candidate in result.candidate_scores]
        assert scores == sorted(scores, reverse=True)
        assert result.candidate_scores[0].word.text == result.selected_word

    def test_a_direct_touch_records_no_cone_candidates(self, two_lines):
        # Resting on "books". Stage 1 answers, so there is no cone to score and
        # `candidate_scores` is empty — the overlay's search-zone box is a
        # debugging aid for stage 2 and has nothing to draw here.
        finger = FingerPoint(x=160.0, y=85.0, confidence=0.9, direction=(0.0, -1.0))
        result = select_intended_word(finger, two_lines, SelectionConfig())

        assert result.selected_word == "books"
        assert result.candidate_scores == ()
        assert "Direct touch" in result.selection_reason


class TestDetector:
    def test_direction_is_a_unit_vector(self):
        dx, dy = estimate_direction((0.0, 100.0), (0.0, 70.0), (0.0, 40.0), (0.0, 10.0))
        assert np.hypot(dx, dy) == pytest.approx(1.0)
        # Joints ascending the image means pointing up, which is negative y.
        assert dy < 0

    def test_a_curled_finger_defaults_to_pointing_up(self):
        # All four joints at one spot: the segments cancel and normalising the
        # result would amplify noise into a confident wrong heading.
        assert estimate_direction((50.0, 50.0), (50.0, 50.0), (50.0, 50.0), (50.0, 50.0)) == (
            0.0,
            -1.0,
        )

    def test_contour_fallback_finds_the_topmost_hull_point(self):
        image = page_with_finger(tip_x=200, tip_y=90)
        point = detect_finger_contour_fallback(image)

        assert point is not None
        assert point.detection_method == "contour_fallback"
        assert point.x == pytest.approx(200.0, abs=12.0)
        assert point.y == pytest.approx(90.0, abs=6.0)

    def test_contour_fallback_confidence_never_outranks_mediapipe(self):
        point = detect_finger_contour_fallback(page_with_finger())
        assert point is not None
        # Capped at 0.5 so the tier that cannot see direction never beats the one
        # that can; the selector widens its cone in response.
        assert 0.1 <= point.confidence <= 0.5

    def test_a_blank_page_has_no_finger(self):
        assert detect_finger_contour_fallback(blank_page()) is None

    def test_small_skin_blobs_are_rejected_as_noise(self):
        image = blank_page()
        cv2.rectangle(image, (100, 100), (110, 110), skin_bgr(), -1)
        # ~100px: wood grain or a warm highlight, not a hand.
        assert detect_finger_contour_fallback(image) is None

    def test_detect_finger_falls_back_when_mediapipe_finds_nothing(self):
        # A drawn rectangle is not a hand, so MediaPipe returns nothing and the
        # contour tier has to answer. This is the path a real session uses
        # whenever the reader's hand is half out of frame.
        point = detect_finger(page_with_finger(), SelectionConfig())
        assert point is not None
        assert point.detection_method == "contour_fallback"


class TestPipeline:
    def test_api_version_is_reported(self):
        assert API_VERSION == "2.0"

    def test_select_word_end_to_end(self, two_lines):
        result = select_word(page_with_finger(tip_x=160, tip_y=80), two_lines)

        assert result.status is SelectionStatus.SUCCESS
        assert result.selected_word == "books"
        assert result.selected_line == "Read books daily."
        assert result.selected_line_words == ("Read", "books", "daily.")
        assert result.line_index == 0
        assert result.paragraph_index == 0
        assert result.context == "Read books daily."
        assert result.image_size == (400, 300)
        assert result.selection_time_ms > 0.0

    def test_empty_ocr_still_reports_the_image_size(self):
        result = select_word(blank_page(), [])
        assert result.status is SelectionStatus.OCR_EMPTY
        assert result.image_size == (400, 300)

    def test_a_selection_publishes_pointer_and_word_events(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))

        pipeline.observe(page_with_finger(tip_x=160, tip_y=80), two_lines)

        names = [event for event, _ in published]
        assert SessionEvent.READING_POINTER_UPDATED in names
        assert SessionEvent.WORD_SELECTED in names
        selected = next(payload for event, payload in published if event is SessionEvent.WORD_SELECTED)
        assert selected["word"] == "books"

    def test_a_meaning_gesture_publishes_only_a_request(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))

        pipeline.observe(
            page_with_finger(tip_x=160, tip_y=80), two_lines, meaning_gesture=True
        )

        names = [event for event, _ in published]
        assert names == [SessionEvent.MEANING_REQUESTED]
        # Asking what a word means is not reading past it, so the pointer must
        # not move — that would credit the reader with progress they did not make.
        assert SessionEvent.READING_POINTER_UPDATED not in names

    def test_the_same_word_twice_publishes_once(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))
        image = page_with_finger(tip_x=160, tip_y=80)

        pipeline.observe(image, two_lines)
        published.clear()
        pipeline.observe(image, two_lines)

        # A resting hand is not a new selection. Republishing would make Reading
        # Speed see repeated progress over the same word.
        assert [event for event, _ in published if event is SessionEvent.WORD_SELECTED] == []

    def test_a_failed_selection_publishes_nothing(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))

        pipeline.observe(blank_page(), two_lines)

        # No fingertip: silence is the correct output. A guess here reaches the
        # reader as a wrong answer read aloud.
        assert published == []

    def test_a_missing_frame_reports_the_camera(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))

        pipeline.observe(None, two_lines)

        assert [event for event, _ in published] == [SessionEvent.CAMERA_OFF]

    def test_reset_lets_the_reader_start_a_page_again(self, two_lines):
        published = []
        pipeline = GesturePipeline(publish=lambda event, payload: published.append((event, payload)))
        image = page_with_finger(tip_x=160, tip_y=80)

        pipeline.observe(image, two_lines)
        pipeline.reset()
        published.clear()
        pipeline.observe(image, two_lines)

        # After a page turn the previous line position is meaningless; without the
        # reset the first selection on the new page reads as a backwards jump.
        assert SessionEvent.READING_POINTER_UPDATED in [event for event, _ in published]

    def test_an_injected_finger_replays_without_a_camera(self, two_lines):
        result = GesturePipeline().process_frame(
            blank_page(),
            two_lines,
            finger=FingerPoint(x=160.0, y=85.0, confidence=0.9, direction=(0.0, -1.0)),
        )
        # The page is blank, so detection would fail; the scenario supplies the
        # gesture instead. This is how recorded sessions replay deterministically.
        assert result.selected_word == "books"

    def test_the_injected_clock_is_used_for_timing(self, two_lines):
        ticks = iter([100.0, 100.25])
        result = GesturePipeline(clock=lambda: next(ticks)).process_frame(blank_page(), [])
        assert result.selection_time_ms == pytest.approx(250.0)


class TestVisualizer:
    def test_every_mode_returns_a_same_shaped_frame(self, two_lines):
        image = page_with_finger(tip_x=160, tip_y=80)
        result = select_word(image, two_lines)

        for mode in ("basic", "developer", "debug"):
            assert draw_overlay(image, result, mode=mode).shape == image.shape

    def test_the_input_frame_is_never_mutated(self, two_lines):
        image = page_with_finger(tip_x=160, tip_y=80)
        original = image.copy()

        draw_overlay(image, select_word(image, two_lines), mode="debug")

        # The frame is shared with OCR and the dashboard; drawing on it in place
        # would put debug rectangles into the OCR input.
        assert np.array_equal(image, original)

    def test_a_result_with_no_finger_still_draws(self):
        image = blank_page()
        overlay = draw_overlay(image, SelectionResult(status=SelectionStatus.NO_FINGER))
        # An empty overlay looks like a crash; the status gets drawn instead.
        assert overlay.shape == image.shape
        assert not np.array_equal(overlay, image)


class TestRegressions:
    """Each test here pins a defect that previously crashed the running system."""

    @pytest.mark.parametrize(
        "strategy",
        [SelectionStrategy.POINT, SelectionStrategy.TOUCH, SelectionStrategy.AUTO],
    )
    def test_a_zero_length_word_does_not_crash_selection(self, strategy):
        # Google Vision emits empty words; indexing `text[-1]` raised IndexError
        # in the sentence splitter and took down the session.
        words = [word("ok", (10, 10, 50, 30)), word("", (60, 10, 90, 30))]
        finger = FingerPoint(x=30.0, y=45.0, confidence=0.9, direction=(0.0, -1.0))

        result = select_intended_word(
            finger, words, SelectionConfig(selection_strategy=strategy)
        )
        assert result.selected_word == "ok"

    def test_google_vision_bbox_is_min_max_not_width_height(self):
        # The controller built (x, y, w, h) boxes, so every x_max was a width and
        # selection scored candidates against geometry that did not exist.
        response = {
            "responses": [
                {
                    "fullTextAnnotation": {
                        "pages": [
                            {
                                "blocks": [
                                    {
                                        "paragraphs": [
                                            {
                                                "words": [
                                                    {
                                                        "symbols": [{"text": "hi"}],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 10, "y": 20},
                                                                {"x": 60, "y": 20},
                                                                {"x": 60, "y": 40},
                                                                {"x": 10, "y": 40},
                                                            ]
                                                        },
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        }

        words = GoogleVisionProvider.parse_response(response)
        assert len(words) == 1
        assert words[0].bbox == (10, 20, 60, 40)
        assert words[0].center_x == pytest.approx(35.0)
        assert words[0].center_y == pytest.approx(30.0)

    def test_provider_output_flows_straight_into_selection(self):
        # The controller passed raw dicts, producing
        # AttributeError: 'dict' object has no attribute 'line_index'.
        # This is the seam that test guards: provider words must be selectable
        # with no conversion step in between.
        words = [word("hello", (10, 20, 60, 40)), word("world", (70, 20, 130, 40))]
        image = blank_page()
        cv2.rectangle(image, (28, 45), (43, 150), skin_bgr(), -1)

        result = select_word(image, words)
        assert result.selected_word == "hello"
        assert result.selected_line == "hello world"

    def test_google_vision_requires_an_explicit_key(self, monkeypatch):
        """No hardcoded key fallback may be reintroduced."""

        monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
        provider = GoogleVisionProvider()
        assert provider.api_key == ""

        from backend.app.modules.ocr.providers import OcrProviderUnavailable

        with pytest.raises(OcrProviderUnavailable, match="GOOGLE_VISION_API_KEY"):
            provider.extract(np.zeros((10, 10, 3), dtype=np.uint8))
