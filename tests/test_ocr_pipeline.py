"""OCR pipeline and Merge Memory tests.

Ported from `backend/app/OCRandGESTURE/Gesture/tests/test_ocr_providers.py` and
`test_google_provider.py`, plus new coverage for the parts that only exist after
integration: the pipeline's four operations, and Merge Memory as the single
source of truth behind them.

Every test runs through `ReplayAdapter`, which is not a mock — it replays
recorded words through the pipeline that runs live, so only the source at the far
end differs. That is the property the port exists to give: `OcrPipeline` cannot
tell which one it got. Replay is not an OCR engine and production cannot select
it; Google Vision is the only engine in production.

No network and no API key. The Google Vision tests exercise `parse_response`
against captured payloads, which is where the parsing bugs actually lived.
"""

import json

import os

import pytest

from backend.app.modules.merge_memory import MergeMemory, Page
from backend.app.modules.ocr.interfaces import OcrProvider
from backend.app.modules.ocr.models import RecognizedWord, words_to_page
from backend.app.modules.ocr.pipeline import (
    MIN_WORDS_FOR_A_PAGE,
    FrameResult,
    OcrPipeline,
)
from backend.app.modules.ocr.providers import (
    GoogleVisionProvider,
    coerce_to_jpeg_bytes,
    get_ocr_engine,
)
from backend.app.modules.ocr.providers import OcrProviderError, OcrProviderUnavailable
from backend.app.modules.ocr.replay import ReplayAdapter
from backend.app.shared.constants import FIRST_PAGE_INDEX
from backend.app.shared.exceptions import StaleContentError


def recorded(*texts, y=20, start_x=10, line_index=0, paragraph_index=0):
    """A page of words laid out left to right on one line."""

    words = []
    x = start_x
    for index, text in enumerate(texts):
        width = 10 * max(1, len(text))
        words.append(
            {
                "text": text,
                "bbox": [x, y, x + width, y + 20],
                "word_index": index,
                "line_index": line_index,
                "paragraph_index": paragraph_index,
            }
        )
        x += width + 10
    return words


def pipeline_over(*pages):
    """An `OcrPipeline` driven by the replay adapter, with preprocessing off.

    Preprocessing is disabled because these pages are word lists, not images —
    there is nothing to sharpen, and CLAHE only applies to raw bytes anyway.
    """

    return OcrPipeline(provider=ReplayAdapter(), preprocess=False)


def vision_page(*paragraphs):
    """A Vision DOCUMENT_TEXT_DETECTION response, written as `(text, break)` pairs.

    The real payload nests six levels deep to say "this word, then a space", and
    written out longhand a four-word test is ninety lines of braces in which the
    one thing under test is invisible. Each word here is `("Son", "SPACE")`, or
    `("Son", None)` for the words Vision reports no break after — which is the
    case the spacing tests exist for.

    Geometry is generated rather than specified: these tests are about spacing,
    and a box that only has to exist should not be written out by hand.
    """

    response_paragraphs = []
    for paragraph in paragraphs:
        words = []
        for index, (text, break_type) in enumerate(paragraph):
            symbols = [{"text": character} for character in text]
            if break_type is not None:
                symbols[-1]["property"] = {"detectedBreak": {"type": break_type}}
            x = 10 + index * 50
            words.append(
                {
                    "symbols": symbols,
                    "boundingBox": {
                        "vertices": [
                            {"x": x, "y": 20},
                            {"x": x + 40, "y": 20},
                            {"x": x + 40, "y": 40},
                            {"x": x, "y": 40},
                        ]
                    },
                }
            )
        response_paragraphs.append({"words": words})

    return {
        "responses": [
            {
                "fullTextAnnotation": {
                    "pages": [{"blocks": [{"paragraphs": response_paragraphs}]}]
                }
            }
        ]
    }


class TestReplayAdapter:
    def test_loads_words_from_a_file(self, tmp_path):
        path = tmp_path / "page.json"
        path.write_text(
            json.dumps(
                [
                    {"text": "Hello", "bbox": [10, 20, 60, 40]},
                    {"text": "World", "bbox": [70, 20, 130, 40], "confidence": 0.95},
                ]
            ),
            encoding="utf-8",
        )

        words = ReplayAdapter().extract(str(path))

        assert [w.text for w in words] == ["Hello", "World"]
        assert words[0].bbox == (10, 20, 60, 40)
        assert words[0].center_x == pytest.approx(35.0)
        assert words[0].center_y == pytest.approx(30.0)
        assert words[0].confidence == 1.0
        assert words[1].confidence == 0.95

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "page.json"
        path.write_text(
            json.dumps(
                [
                    {"text": "Good", "bbox": [10, 20, 50, 40]},
                    {"text": "Bad", "bbox": [10, 20]},
                    {"text": "", "bbox": [10, 20, 50, 40]},
                    {"bbox": [10, 20, 50, 40]},
                ]
            ),
            encoding="utf-8",
        )

        # A recorded page with one bad row should still replay. Raising here
        # would make a single malformed entry unreplayable.
        assert [w.text for w in ReplayAdapter().extract(str(path))] == ["Good"]

    def test_a_missing_file_is_a_provider_error(self):
        # Not FileNotFoundError: the caller catches OcrProviderError to decide
        # whether to retry, and a provider that raises OS exceptions makes every
        # call site handle two families.
        with pytest.raises(OcrProviderError, match="not found"):
            ReplayAdapter().extract("no_such_page_xyz.json")

    def test_an_empty_page_is_empty_not_an_error(self, tmp_path):
        path = tmp_path / "page.json"
        path.write_text("[]", encoding="utf-8")
        assert ReplayAdapter().extract(str(path)) == []

    def test_optional_indices_survive_the_round_trip(self, tmp_path):
        path = tmp_path / "page.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "text": "idx",
                        "bbox": [10, 20, 50, 40],
                        "line_index": 3,
                        "word_index": 1,
                        "paragraph_index": 2,
                    }
                ]
            ),
            encoding="utf-8",
        )

        word = ReplayAdapter().extract(str(path))[0]
        assert (word.word_index, word.line_index, word.paragraph_index) == (1, 3, 2)

    def test_a_word_list_can_be_passed_directly(self):
        # Skips the file entirely, which is how a scenario builds a page inline.
        words = ReplayAdapter().extract(recorded("inline", "page"))
        assert [w.text for w in words] == ["inline", "page"]


class TestProductionEngineSelection:
    """Provider selection: OCR.Space (primary) and Google Vision (fallback)."""

    def test_ocr_space_is_selected_when_its_key_is_set(self, monkeypatch):
        monkeypatch.setenv("OCR_SPACE_API_KEY", "key")
        monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
        assert get_ocr_engine().provider_name == "ocr_space"

    def test_vision_is_selected_when_only_its_key_is_set(self, monkeypatch):
        monkeypatch.delenv("OCR_SPACE_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_VISION_API_KEY", "key")
        assert get_ocr_engine().provider_name == "google_vision"

    def test_ocr_space_takes_priority_over_vision(self, monkeypatch):
        monkeypatch.setenv("OCR_SPACE_API_KEY", "ocr-key")
        monkeypatch.setenv("GOOGLE_VISION_API_KEY", "vision-key")
        assert get_ocr_engine().provider_name == "ocr_space"

    def test_no_environment_variable_can_change_the_engine(self, monkeypatch):
        # The whole point of dropping the string registry: a stray or misspelt variable
        # must not be able to change which engine reads the page.
        monkeypatch.setenv("OCR_PROVIDER", "paddle")
        monkeypatch.setenv("OCR_SPACE_API_KEY", "key")
        assert get_ocr_engine().provider_name == "ocr_space"

    def test_the_replay_adapter_is_not_reachable_from_production(self):
        # Replay must be constructed explicitly by a test or demo. It keeps a
        # `provider_name` because the port requires one, but the guarantee is
        # that no lookup maps a string to it: `get_ocr_engine` takes no name, and
        # there is no registry left to consult.
        import inspect

        from backend.app.modules.ocr import providers

        # No registry to consult and no name to pass: the only way to reach
        # replay is to import and construct it, which production never does.
        assert not [name for name in dir(providers) if "PROVIDERS" in name]
        assert not hasattr(providers, "ReplayAdapter")
        assert list(inspect.signature(get_ocr_engine).parameters) == ["kwargs"]

    def test_resolving_the_engine_does_not_check_credentials(self, monkeypatch):
        monkeypatch.delenv("OCR_SPACE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
        # Constructing the runtime must not require a network call or a key;
        # the failure belongs at the first frame, not at startup.  It defaults
        # to OCR.Space so the error names the prototype key.
        assert get_ocr_engine().provider_name == "ocr_space"

    def test_both_sources_satisfy_the_port(self):
        # Structural, not inheritance: the pipeline accepts anything with the
        # methods, which is what lets replay stand in for Vision without the
        # pipeline knowing.
        from backend.app.modules.ocr.ocr_space import OcrSpaceProvider
        for source in (
            GoogleVisionProvider(api_key="x"), 
            OcrSpaceProvider(api_key="x"),
            ReplayAdapter()
        ):
            assert isinstance(source, OcrProvider)

    def test_the_sources_disagree_about_what_they_accept(self):
        # The reason `accepts` is on the port at all. Vision can be handed raw
        # JPEG bytes; replay reads recorded responses, not images.
        assert GoogleVisionProvider(api_key="x").accepts(b"jpeg-bytes")
        assert not ReplayAdapter().accepts(b"jpeg-bytes")

class TestOcrSpaceProvider:
    """Unit tests for the new OCR.Space adapter."""

    def test_missing_key_raises_unavailable(self):
        # We ensure the env var is gone, and we don't pass it to __init__
        import os
        from backend.app.modules.ocr.ocr_space import OcrSpaceProvider
        
        provider = OcrSpaceProvider(api_key="")
        # Force api_key empty in case the env var slipped through
        provider.api_key = ""
        
        with pytest.raises(OcrProviderUnavailable, match="OCR_SPACE_API_KEY is not set"):
            provider.extract(b"dummy")

    def test_image_larger_than_one_megabyte_is_rejected(self):
        from backend.app.modules.ocr.ocr_space import OcrSpaceProvider
        
        provider = OcrSpaceProvider(api_key="dummy")
        huge_image = b"0" * 1_048_577  # 1 byte over 1MB
        
        with pytest.raises(OcrProviderError, match="exceeds the OCR.Space free-tier limit"):
            provider.extract(huge_image)

    def test_paragraph_grouping_by_vertical_gap(self):
        from backend.app.modules.ocr.ocr_space import _assign_paragraphs
        
        # Scenario: two lines close together (y=100, 115), one line far away (y=200)
        # Median height is 10. Threshold is 1.5 * 10 = 15.
        y_centers = [100.0, 115.0, 200.0]
        heights = [10.0, 10.0, 10.0]
        
        paragraphs = _assign_paragraphs(y_centers, heights)
        
        # First two lines are one paragraph (gap 15 <= 15)
        # Third line is a new paragraph (gap 85 > 15)
        assert paragraphs == [0, 0, 1]

    @pytest.mark.skipif(
        not os.environ.get("OCR_SPACE_API_KEY"), 
        reason="Real OCR.Space smoke test requires OCR_SPACE_API_KEY"
    )
    def test_real_ocr_space_smoke_test(self):
        # An opt-in integration test that actually hits OCR.Space Engine 2
        # Requires OCR_SPACE_API_KEY. Skips otherwise.
        from backend.app.modules.ocr.ocr_space import OcrSpaceProvider
        
        provider = OcrSpaceProvider()
        # Create a tiny 50x50 white JPEG to send
        import cv2
        import numpy as np
        img = np.full((50, 50, 3), 255, dtype=np.uint8)
        
        # Just getting a result back (even 0 words) proves the API auth and
        # request format are correct.
        words = provider.extract(img)
        assert isinstance(words, list)

    def test_replay_uses_the_production_parser(self):
        # Not a second parser: a recorded Vision response replays through
        # `GoogleVisionProvider.parse_response`, so replayed words are parsed by
        # the same code production uses.
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
                                                        "symbols": [
                                                            {"text": "R"},
                                                            {"text": "e"},
                                                            {"text": "a"},
                                                            {"text": "l"},
                                                        ],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 10, "y": 20},
                                                                {"x": 60, "y": 20},
                                                                {"x": 60, "y": 40},
                                                                {"x": 10, "y": 40},
                                                            ]
                                                        },
                                                        "confidence": 0.99,
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
        replayed = ReplayAdapter().extract(response)
        directly = GoogleVisionProvider.parse_response(response)

        assert [w.text for w in replayed] == ["Real"]
        assert [(w.text, w.bbox, w.paragraph_index) for w in replayed] == [
            (w.text, w.bbox, w.paragraph_index) for w in directly
        ]

    def test_jpeg_bytes_pass_through_unre_encoded(self):
        payload = b"\xff\xd8\xff\xe0 pretend jpeg"
        # The ESP32 already sends JPEG. A decode/encode round trip would cost
        # image quality for no gain.
        assert coerce_to_jpeg_bytes(payload) is payload

    def test_an_unsupported_source_type_is_rejected(self):
        with pytest.raises(OcrProviderError, match="Unsupported image source"):
            coerce_to_jpeg_bytes(42)


class TestGoogleVisionParsing:
    def test_document_text_annotation_carries_structure(self):
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
                                                        "symbols": [
                                                            {"text": "H"},
                                                            {"text": "i"},
                                                            {
                                                                "text": "!",
                                                                "property": {
                                                                    "detectedBreak": {
                                                                        "type": "SPACE"
                                                                    }
                                                                },
                                                            },
                                                        ],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 10, "y": 20},
                                                                {"x": 60, "y": 20},
                                                                {"x": 60, "y": 40},
                                                                {"x": 10, "y": 40},
                                                            ]
                                                        },
                                                        "confidence": 0.99,
                                                    },
                                                    {
                                                        "symbols": [
                                                            {"text": "y"},
                                                            {"text": "o"},
                                                            {
                                                                "text": "u",
                                                                "property": {
                                                                    "detectedBreak": {
                                                                        "type": "LINE_BREAK"
                                                                    }
                                                                },
                                                            },
                                                        ],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 70, "y": 20},
                                                                {"x": 130, "y": 20},
                                                                {"x": 130, "y": 40},
                                                                {"x": 70, "y": 40},
                                                            ]
                                                        },
                                                        "confidence": 0.95,
                                                    },
                                                ]
                                            },
                                            {
                                                "words": [
                                                    {
                                                        "symbols": [{"text": "Next"}],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 10, "y": 50},
                                                                {"x": 80, "y": 50},
                                                                {"x": 80, "y": 70},
                                                                {"x": 10, "y": 70},
                                                            ]
                                                        },
                                                        "confidence": 0.90,
                                                    }
                                                ]
                                            },
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

        assert [w.text for w in words] == ["Hi!", "you", "Next"]
        assert words[0].bbox == (10, 20, 60, 40)
        assert words[0].confidence == pytest.approx(0.99)
        assert (words[0].line_index, words[0].paragraph_index) == (0, 0)
        # A SPACE break keeps the line; only LINE_BREAK advances it.
        assert words[1].line_index == 0
        assert words[2].paragraph_index == 1

    def test_plain_text_detection_falls_back_without_inventing_structure(self):
        response = {
            "responses": [
                {
                    "textAnnotations": [
                        {"description": "Hello World"},
                        {
                            "description": "Hello",
                            "boundingPoly": {
                                "vertices": [
                                    {"x": 10, "y": 20},
                                    {"x": 60, "y": 20},
                                    {"x": 60, "y": 40},
                                    {"x": 10, "y": 40},
                                ]
                            },
                            "confidence": 0.98,
                        },
                    ]
                }
            ]
        }

        words = GoogleVisionProvider.parse_response(response)

        assert [w.text for w in words] == ["Hello"]
        # -1, not 0. This path carries no paragraph structure, and a fabricated
        # zero would tell the selector every word shares a paragraph.
        assert words[0].paragraph_index == -1
        assert words[0].line_index == -1

    @pytest.mark.parametrize(
        "response", [{}, {"responses": []}, {"responses": [{}]}]
    )
    def test_empty_and_malformed_responses_yield_no_words(self, response):
        assert GoogleVisionProvider.parse_response(response) == []

    def test_confidence_falls_back_to_the_symbol_average(self):
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
                                                        "symbols": [
                                                            {"text": "a", "confidence": 0.8},
                                                            {"text": "b", "confidence": 0.6},
                                                        ],
                                                        "boundingBox": {
                                                            "vertices": [
                                                                {"x": 0, "y": 0},
                                                                {"x": 10, "y": 0},
                                                                {"x": 10, "y": 10},
                                                                {"x": 0, "y": 10},
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

        assert GoogleVisionProvider.parse_response(response)[0].confidence == pytest.approx(0.7)


class TestSpacingFollowsVision:
    """Page text must match what Vision's own flat `text` would have said.

    The reference implementation read `fullTextAnnotation.text` and got spacing
    for free. That string cannot be used here — it carries no paragraph structure
    at all — so text is rebuilt from the word hierarchy, and these tests pin the
    one thing the flat string did better.

    Verified against live Vision on three real page photographs: word-for-word
    identical to the reference's flat text on all three, from the same response.
    An earlier version guessed spacing from the character class and scored 90% on
    a dialogue-heavy page, because `"` is the same character opening and closing.
    """

    def render(self, response) -> str:
        """The page as the runtime would hold it, through the production parser.

        Via the pipeline rather than by calling the renderer directly, because a
        recorded Vision response is exactly what `ReplayAdapter` hands to
        `GoogleVisionProvider.parse_response` — the same parser production uses.
        Asserting on the renderer alone would leave the plumbing between them
        untested, and that plumbing is where the `space_after` field travels.
        """

        pipeline = pipeline_over()
        pipeline.update_memory(response)
        return pipeline.text

    def test_a_break_type_becomes_the_space_after_flag(self):
        words = GoogleVisionProvider.parse_response(
            vision_page([("Hi", "SPACE"), ("there", None)])
        )

        assert words[0].space_after is True
        # No `detectedBreak` at all is Vision saying the next word hugs this one.
        assert words[1].space_after is False

    @pytest.mark.parametrize("break_type", ["SPACE", "SURE_SPACE", "EOL_SURE_SPACE", "LINE_BREAK"])
    def test_every_break_that_means_a_space_is_honoured(self, break_type):
        assert self.render(vision_page([("one", break_type), ("two", None)])) == "one two"

    @pytest.mark.parametrize("break_type", [None, "HYPHEN"])
    def test_breaks_that_close_a_word_up_produce_no_space(self, break_type):
        # HYPHEN is a word split across a printed line end; absent is punctuation
        # hugging its neighbour. Neither may leave a space behind.
        assert self.render(vision_page([("one", break_type), ("two", None)])) == "onetwo"

    def test_quotes_close_around_the_words_they_enclose(self):
        # The case that character classes cannot decide: the same `"` opens and
        # closes, so the direction it binds is only knowable from the break.
        rendered = self.render(
            vision_page(
                [
                    ('"', None),
                    ("Son", None),
                    ("!", None),
                    ('"', "SPACE"),
                    ("said", "SPACE"),
                    ("Fels", None),
                ]
            )
        )

        assert rendered == '"Son!" said Fels'

    def test_an_ellipsis_binds_on_both_sides(self):
        rendered = self.render(
            vision_page(
                [("Um", None), ("...", None), ("er", None), ("...", "SPACE"), ("yes", None)]
            )
        )

        assert rendered == "Um...er... yes"

    def test_a_word_hyphenated_across_a_line_end_closes_up(self):
        rendered = self.render(
            vision_page([("advance", "HYPHEN"), ("ments", "SPACE"), ("are", None)])
        )

        assert rendered == "advancements are"

    def test_paragraphs_survive_the_spacing_rules(self):
        rendered = self.render(
            vision_page(
                [("First", "SPACE"), ("paragraph", None)],
                [("Second", "SPACE"), ("one", None)],
            )
        )

        # Merge Memory splits on a blank line to rebuild the page's shape, so the
        # paragraph break has to survive whatever the spacing rules did inside it.
        assert rendered == "First paragraph\n\nSecond one"

    def test_a_source_that_reports_no_breaks_still_spaces_its_words(self):
        # The replay adapter's fixture path builds words by hand and knows nothing
        # about breaks, so `space_after` defaults to True. Character classes remain
        # as the fallback there: they only ever join more, never less.
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("Two", "topics", "impact", "everyone", "here"))

        assert pipeline.text == "Two topics impact everyone here"

    def test_the_fallback_still_reattaches_punctuation_vision_split_out(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("realize", ",", "greatly", "under", "-", "rated"))

        assert pipeline.text == "realize, greatly under-rated"


class TestWordConversion:
    def test_a_word_converts_to_the_domain_contract(self):
        word = RecognizedWord.from_bbox("word", (10, 20, 60, 50), confidence=0.9)
        domain = word.to_domain()

        assert domain.text == "word"
        assert domain.bounding_box.x == 10.0
        # The domain model stores width/height, not a second corner.
        assert domain.bounding_box.width == 50.0
        assert domain.bounding_box.height == 30.0

    def test_out_of_range_confidence_is_clamped_not_raised(self):
        # Providers occasionally report 1.0000001. A validation error mid-session
        # is a worse outcome than a rounded score.
        assert RecognizedWord.from_bbox("w", (0, 0, 1, 1), confidence=1.4).to_domain().confidence == 1.0

    def test_unassigned_words_become_a_trailing_paragraph(self):
        words = [
            RecognizedWord.from_bbox("first", (0, 0, 10, 10), paragraph_index=0),
            RecognizedWord.from_bbox("loose", (0, 20, 10, 30), paragraph_index=-1),
        ]
        page = words_to_page(words)

        # Dropped words would look downstream like a shorter page rather than a
        # page read poorly.
        assert len(page.paragraphs) == 2
        assert page.paragraphs[-1].text == "loose"


class TestPipeline:
    def test_a_first_frame_is_accepted_and_versioned(self):
        pipeline = pipeline_over()
        result = pipeline.update_memory(recorded("The", "quick", "brown", "fox"))

        assert result.accepted
        assert result.version == 1
        assert result.provider == "replay"
        assert result.text == "The quick brown fox"

    def test_a_second_frame_merges_rather_than_replaces(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("The", "quick", "brown", "fox"))
        result = pipeline.update_memory(recorded("The", "quick", "brown", "fox", "jumps"))

        assert result.accepted
        assert not result.page_changed
        assert "jumps" in result.text
        assert result.version == 2

    def test_a_blank_frame_is_ignored_without_losing_the_page(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("Held", "text"))
        result = pipeline.update_memory([])

        assert not result.accepted
        assert result.reason == "no text detected in frame"
        # A blurred frame is routine; it must not blank the page.
        assert result.text == "Held text"
        assert result.version == 1

    def test_a_provider_failure_is_reported_not_raised(self):
        class FailingProvider:
            provider_name = "failing"

            def accepts(self, source):
                return True

            def extract(self, source):
                raise OcrProviderError("vision API timed out")

        result = OcrPipeline(provider=FailingProvider(), preprocess=False).update_memory("frame")

        # The runtime's correct response is to retry the next frame, so this
        # returns rather than raising through the session loop.
        assert not result.accepted
        assert "timed out" in result.reason

    def test_a_source_the_provider_cannot_read_is_rejected(self):
        result = OcrPipeline(provider=ReplayAdapter(), preprocess=False).update_memory(b"jpeg")
        assert not result.accepted
        assert "cannot read" in result.reason

    def test_a_wholly_different_page_is_a_page_turn(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("Chapter", "one", "begins", "here", "now", "today"))
        result = pipeline.update_memory(
            recorded("Chapter", "two", "different", "entirely", "fresh", "words")
        )

        assert result.page_changed
        assert result.reason == "page turn detected"
        # Replaced, not merged: merging across a turn produces text that reads as
        # two pages interleaved.
        assert "begins" not in result.text

    def test_a_shifted_view_of_the_same_page_is_not_a_page_turn(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("alpha", "beta", "gamma", "delta", "epsilon", "zeta"))
        result = pipeline.update_memory(recorded("gamma", "delta", "epsilon", "zeta", "eta", "theta"))

        # A camera nudge drops words out of view. Sharing a third of the frame is
        # far more likely to be a shift than a turn.
        assert not result.page_changed

    def test_a_handful_of_stray_words_cannot_trigger_a_page_turn(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("The", "real", "page", "of", "text", "here"))
        stray = recorded(*[f"noise{i}" for i in range(MIN_WORDS_FOR_A_PAGE - 1)])

        # A hand across the book produces a few spurious words. Letting them
        # count would commit the real page and reset the pointer.
        assert not pipeline.detect_new_page(pipeline.process_frame(stray))

    def test_the_version_survives_a_page_turn(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("Page", "one", "words", "here", "now", "again"))
        before = pipeline.version
        result = pipeline.update_memory(recorded("Page", "two", "entirely", "new", "words", "appear"))

        # A version that reset would make the first frame of page 2 look older
        # than the last frame of page 1, and consumers reject stale versions.
        assert result.version == before + 1

    def test_reset_drops_the_page_but_keeps_the_version(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("Some", "text"))
        version = pipeline.version
        pipeline.reset()

        assert pipeline.words == ()
        assert pipeline.version == version

    def test_the_higher_confidence_reading_of_a_word_wins(self):
        pipeline = pipeline_over()
        clear = [{"text": "clear", "bbox": [10, 20, 60, 40], "confidence": 0.95}]
        shadowed = [{"text": "clear", "bbox": [10, 20, 60, 40], "confidence": 0.40}]

        pipeline.update_memory(clear)
        pipeline.update_memory(shadowed)

        # A later frame is not automatically better: the reader's hand moves, and
        # half a page read clearly should not be replaced by the same half read
        # through a shadow.
        assert pipeline.words[0].confidence == pytest.approx(0.95)

    def test_process_frame_changes_no_state(self):
        pipeline = pipeline_over()
        pipeline.process_frame(recorded("Nothing", "retained"))
        # Separated from update_memory precisely so a scenario can provoke
        # recognition without advancing the page.
        assert pipeline.words == ()
        assert pipeline.version == 0

    def test_a_page_turn_is_reported_not_acted_on(self):
        pipeline = pipeline_over()
        pipeline.update_memory(recorded("First", "page", "of", "the", "book", "here"))
        result = pipeline.update_memory(recorded("Second", "page", "entirely", "new", "text", "now"))

        # OCR must not commit the page or move the pointer. Reading Speed wants to
        # close an observation and Audio wants to rebuild its queue; ordering that
        # is the runtime's job.
        assert isinstance(result, FrameResult)
        assert result.page_changed is True


class TestMergeMemory:
    def test_a_frame_becomes_page_text(self):
        memory = MergeMemory()
        version = memory.apply_frame("The opening line of the book.")

        assert version == 1
        assert memory.page_text(FIRST_PAGE_INDEX) == "The opening line of the book."
        assert memory.page_count == 1

    def test_frames_accumulate_into_one_page(self):
        memory = MergeMemory()
        memory.apply_frame("First paragraph.")
        memory.apply_frame("Second paragraph.")

        assert memory.paragraph_count(FIRST_PAGE_INDEX) == 2
        assert memory.paragraph(FIRST_PAGE_INDEX, 1) == "Second paragraph."
        assert memory.version == 2

    def test_a_stale_frame_is_rejected(self):
        memory = MergeMemory()
        memory.apply_frame("Newer text")
        memory.apply_frame("Newer still")

        # Frames arrive over HTTP and overtake each other. Silently accepting an
        # older one overwrites newer text with worse text.
        with pytest.raises(StaleContentError):
            memory.apply_frame("Older text", frame_version=0)

    def test_an_empty_frame_does_not_bump_the_version(self):
        memory = MergeMemory()
        memory.apply_frame("Real text")
        assert memory.apply_frame("   ") == 1

    def test_a_reconstructor_is_used_when_supplied(self):
        memory = MergeMemory(reconstruct=lambda held, incoming: f"{held} {incoming} [joined]")
        memory.apply_frame("A sentence broken")
        memory.apply_frame("across two frames.")

        assert "[joined]" in memory.page_text(FIRST_PAGE_INDEX)

    def test_without_a_reconstructor_text_still_accumulates(self):
        memory = MergeMemory()
        memory.apply_frame("First")
        memory.apply_frame("Second")

        # A session with no API key degrades to rougher text, never to no text.
        assert "First" in memory.page_text(FIRST_PAGE_INDEX)
        assert "Second" in memory.page_text(FIRST_PAGE_INDEX)

    def test_a_whole_page_replaces_rather_than_appends(self):
        """The OCR pipeline's output is a merged page, not a fragment of one.

        Appending it accumulates an already-accumulated page: the same words
        again on every frame, growing without bound while OCR's own word count
        stays still. This is the flag that keeps the two accumulators from
        double-counting.
        """

        memory = MergeMemory()
        memory.apply_frame("The lighthouse stood alone.", whole_page=True)
        memory.apply_frame("The lighthouse stood alone on the cliff.", whole_page=True)

        assert memory.page_text(FIRST_PAGE_INDEX) == "The lighthouse stood alone on the cliff."
        assert memory.paragraph_count(FIRST_PAGE_INDEX) == 1
        # Still two frames and two versions: the page was superseded, not ignored.
        assert memory.version == 2

    def test_a_whole_page_still_goes_through_the_reconstructor(self):
        """`whole_page` chooses replace over append. It does not skip cleaning.

        What the reconstructor does is repair raw OCR — drop the running header
        and the page number, close up a word the camera broke, restore characters
        Vision lost. A page that arrived whole needs all of that as much as a
        fragment does, and the reference ran it on every frame for exactly that
        reason. What `whole_page` changes is what it is merged *against*: nothing,
        rather than the page already held, because OCR has already accumulated it.
        """

        memory = MergeMemory(reconstruct=lambda held, incoming: f"{held}{incoming} [cleaned]")
        memory.apply_frame("A whole page.", whole_page=True)
        memory.apply_frame("A whole page, read better.", whole_page=True)

        # Replaced, not accumulated — and cleaned on the way through.
        assert memory.page_text(FIRST_PAGE_INDEX) == "A whole page, read better. [cleaned]"

    def test_a_whole_page_is_reconstructed_against_nothing(self):
        """Not against the page held, which OCR has already merged this frame into.

        Passing the held page here would ask the model to merge an accumulated
        page with itself, and it would either duplicate the overlap or drop it.
        """

        seen: list[tuple[str, str]] = []

        def record(held: str, incoming: str) -> str:
            seen.append((held, incoming))
            return incoming

        memory = MergeMemory(reconstruct=record)
        memory.apply_frame("First reading of the page.", whole_page=True)
        memory.apply_frame("Second, better reading of the page.", whole_page=True)

        assert seen == [
            ("", "First reading of the page."),
            ("", "Second, better reading of the page."),
        ]

    def test_a_fragment_is_reconstructed_against_the_page_held(self):
        """The seam case, unchanged: a fragment is joined to what came before."""

        seen: list[tuple[str, str]] = []

        def record(held: str, incoming: str) -> str:
            seen.append((held, incoming))
            return f"{held} {incoming}".strip()

        memory = MergeMemory(reconstruct=record)
        memory.apply_frame("A sentence broken")
        memory.apply_frame("across two frames.")

        assert seen == [
            ("", "A sentence broken"),
            ("A sentence broken", "across two frames."),
        ]
        assert memory.page_text(FIRST_PAGE_INDEX) == "A sentence broken across two frames."

    def test_beginning_a_page_commits_the_one_being_left(self):
        memory = MergeMemory()
        memory.apply_frame("Page one text.")
        memory.begin_page(FIRST_PAGE_INDEX + 1)
        memory.apply_frame("Page two text.")

        committed = memory.committed_pages()
        assert [page.page_index for page in committed] == [FIRST_PAGE_INDEX]
        assert memory.current_page == FIRST_PAGE_INDEX + 1

    def test_an_empty_page_is_never_committed(self):
        memory = MergeMemory()
        # An empty page in history would be counted by the session summary as a
        # page the reader read.
        assert memory.commit_page(FIRST_PAGE_INDEX) is False
        assert memory.committed_pages() == []

    def test_committing_twice_is_a_no_op(self):
        memory = MergeMemory()
        memory.apply_frame("Text")
        assert memory.commit_page(FIRST_PAGE_INDEX) is True
        assert memory.commit_page(FIRST_PAGE_INDEX) is False

    def test_reading_an_unknown_page_returns_empty_not_an_error(self):
        memory = MergeMemory()
        assert memory.paragraph(FIRST_PAGE_INDEX + 9, 0) == ""
        assert memory.paragraph_count(FIRST_PAGE_INDEX + 9) == 0
        assert memory.page_text(FIRST_PAGE_INDEX + 9) == ""

    def test_the_content_map_describes_every_known_page(self):
        memory = MergeMemory()
        memory.apply_frame("One sentence. Two sentences.")
        memory.begin_page(FIRST_PAGE_INDEX + 1)
        memory.apply_frame("A third sentence on the next page.")

        content = memory.content_map()

        assert set(content.page_word_counts) == {FIRST_PAGE_INDEX, FIRST_PAGE_INDEX + 1}
        assert len(content.sentences) >= 3
        assert {span.pointer.page_index for span in content.sentences} == {FIRST_PAGE_INDEX, FIRST_PAGE_INDEX + 1}

    def test_the_content_map_uses_the_audio_engine_segmenter(self):
        from backend.app.modules.audio_engine.sentence_queue import segment_sentences
        from backend.app.modules.reading_speed.models import ReadingPointer

        text = "Dr. Smith arrived at 9 a.m. He was late."
        memory = MergeMemory()
        memory.apply_frame(text)

        expected = segment_sentences(text, start_pointer=ReadingPointer())
        # A sentence index has to mean the same thing to Merge Memory and the
        # Audio Engine. Two segmenters would make "sentence 3" ambiguous.
        assert len(memory.content_map().sentences) == len(expected)

    def test_word_counts_track_what_was_merged(self):
        memory = MergeMemory()
        memory.apply_frame("one two three")
        memory.apply_frame("four five")

        assert memory.total_words == 5

    def test_frames_merged_records_how_much_was_seen(self):
        memory = MergeMemory()
        memory.apply_frame("first look")
        memory.apply_frame("second look")

        page = memory.committed_pages() or [Page(page_index=0)]
        memory.commit_page(FIRST_PAGE_INDEX)
        # A page built from one blurred frame and one built from twelve clean
        # ones are both "known"; a consumer deciding whether to trust the text
        # needs to tell them apart.
        assert memory.committed_pages()[0].frames_merged == 2

    def test_page_text_collapses_wrapped_lines(self):
        memory = MergeMemory()
        memory.apply_frame("a line\nwrapped mid sentence\n\nA new paragraph.")

        # A single newline is a line wrap inside a paragraph; a blank line is a
        # real break. Collapsing the former keeps a sentence whole.
        assert memory.paragraph_count(FIRST_PAGE_INDEX) == 2
        assert memory.paragraph(FIRST_PAGE_INDEX, 0) == "a line wrapped mid sentence"


class TestPipelineIntoMemory:
    """The seam the integration exists to create: one pipeline, one memory."""

    def test_ocr_output_flows_into_merge_memory(self):
        pipeline = pipeline_over()
        memory = MergeMemory()

        result = pipeline.update_memory(recorded("The", "opening", "line."))
        memory.apply_frame(result.text, frame_version=result.version)

        assert memory.page_text(FIRST_PAGE_INDEX) == "The opening line."
        assert memory.version == 1

    def test_a_page_turn_commits_the_previous_page(self):
        pipeline = pipeline_over()
        memory = MergeMemory()

        first = pipeline.update_memory(recorded("Page", "one", "has", "these", "words", "here"))
        memory.apply_frame(first.text)

        second = pipeline.update_memory(recorded("Page", "two", "entirely", "fresh", "text", "now"))
        assert second.page_changed

        # The runtime — not OCR — reacts to the reported turn.
        memory.begin_page(memory.current_page + 1)
        memory.apply_frame(second.text)
        pipeline.reset()

        assert [page.page_index for page in memory.committed_pages()] == [FIRST_PAGE_INDEX]
        assert memory.current_page == FIRST_PAGE_INDEX + 1
        assert "Page two entirely fresh text now" == memory.page_text(FIRST_PAGE_INDEX + 1)

    def test_an_ignored_frame_leaves_memory_untouched(self):
        pipeline = pipeline_over()
        memory = MergeMemory()

        first = pipeline.update_memory(recorded("Real", "text"))
        memory.apply_frame(first.text, frame_version=first.version)

        blank = pipeline.update_memory([])
        if blank.accepted:  # pragma: no cover - guards the test's own premise
            memory.apply_frame(blank.text, frame_version=blank.version)

        assert memory.version == 1
        assert memory.page_text(FIRST_PAGE_INDEX) == "Real text"

    def test_paragraph_structure_survives_the_pipeline_to_memory_seam(self):
        # Regression: pipeline.text used to join every word with a space,
        # collapsing a 40-paragraph page into one run before Merge Memory
        # ever saw it. Words with different paragraph_index values must
        # arrive in Merge Memory as separate paragraphs.
        pipeline = pipeline_over()
        memory = MergeMemory()

        para0 = recorded("First", "paragraph", "here", y=20, paragraph_index=0)
        para1 = recorded("Second", "paragraph", "here", y=80, line_index=1, paragraph_index=1)
        result = pipeline.update_memory(para0 + para1)
        memory.apply_frame(result.text, frame_version=result.version, whole_page=True)

        assert memory.paragraph_count(FIRST_PAGE_INDEX) == 2
        assert memory.paragraph(FIRST_PAGE_INDEX, 0) == "First paragraph here"
        assert memory.paragraph(FIRST_PAGE_INDEX, 1) == "Second paragraph here"
