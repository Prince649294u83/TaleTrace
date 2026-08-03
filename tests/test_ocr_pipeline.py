"""OCR pipeline and Merge Memory tests.

Ported from `backend/app/OCRandGESTURE/Gesture/tests/test_ocr_providers.py` and
`test_google_provider.py`, plus new coverage for the parts that only exist after
integration: the pipeline's four operations, and Merge Memory as the single
source of truth behind them.

Every test runs through `JsonOcrProvider`, which is not a mock — it is the real
provider replaying recorded words, so the pipeline under test is the one that
runs live. Only the engine at the far end differs. That is the property the
provider port exists to give: `OcrPipeline` cannot tell which one it got.

No network and no API key. The Google Vision tests exercise `parse_response`
against captured payloads, which is where the parsing bugs actually lived.
"""

import json

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
    JsonOcrProvider,
    OcrProviderError,
    OcrProviderUnavailable,
    coerce_to_jpeg_bytes,
    get_provider,
)
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
    """An `OcrPipeline` driven by the JSON provider, with preprocessing off.

    Preprocessing is disabled because these pages are word lists, not images —
    there is nothing to sharpen, and CLAHE only applies to raw bytes anyway.
    """

    return OcrPipeline(provider=JsonOcrProvider(), preprocess=False)


class TestJsonProvider:
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

        words = JsonOcrProvider().extract(str(path))

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
        assert [w.text for w in JsonOcrProvider().extract(str(path))] == ["Good"]

    def test_a_missing_file_is_a_provider_error(self):
        # Not FileNotFoundError: the caller catches OcrProviderError to decide
        # whether to retry, and a provider that raises OS exceptions makes every
        # call site handle two families.
        with pytest.raises(OcrProviderError, match="not found"):
            JsonOcrProvider().extract("no_such_page_xyz.json")

    def test_an_empty_page_is_empty_not_an_error(self, tmp_path):
        path = tmp_path / "page.json"
        path.write_text("[]", encoding="utf-8")
        assert JsonOcrProvider().extract(str(path)) == []

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

        word = JsonOcrProvider().extract(str(path))[0]
        assert (word.word_index, word.line_index, word.paragraph_index) == (1, 3, 2)

    def test_a_word_list_can_be_passed_directly(self):
        # Skips the file entirely, which is how a scenario builds a page inline.
        words = JsonOcrProvider().extract(recorded("inline", "page"))
        assert [w.text for w in words] == ["inline", "page"]


class TestProviderPort:
    def test_every_provider_satisfies_the_port(self):
        # Structural, not inheritance: a provider satisfies the contract by
        # having the methods, so adding an engine never means editing a base
        # class the other two share.
        for provider in (GoogleVisionProvider(api_key="x"), JsonOcrProvider()):
            assert isinstance(provider, OcrProvider)

    def test_providers_disagree_about_what_they_accept(self):
        # The reason `accepts` is on the port at all. Google Vision can be handed
        # a URL; the JSON provider cannot read one.
        assert GoogleVisionProvider(api_key="x").accepts(b"jpeg-bytes")
        assert not JsonOcrProvider().accepts(b"jpeg-bytes")

    def test_get_provider_resolves_by_name(self):
        assert get_provider("json").provider_name == "json"

    def test_get_provider_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("OCR_PROVIDER", "json")
        assert get_provider().provider_name == "json"

    def test_an_unknown_provider_names_the_alternatives(self):
        with pytest.raises(OcrProviderUnavailable, match="Available:"):
            get_provider("tesseract")

    def test_resolving_a_provider_does_not_check_credentials(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
        # Constructing the runtime must not require a network call or a key;
        # the failure belongs at the first frame, not at startup.
        assert get_provider("google_vision").provider_name == "google_vision"

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
        assert result.provider == "json"
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
        result = OcrPipeline(provider=JsonOcrProvider(), preprocess=False).update_memory(b"jpeg")
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

    def test_a_whole_page_does_not_go_through_the_reconstructor(self):
        """There is no seam to repair when the incoming text is the entire page."""

        memory = MergeMemory(reconstruct=lambda held, incoming: f"{held} {incoming} [joined]")
        memory.apply_frame("A whole page.", whole_page=True)
        memory.apply_frame("A whole page, read better.", whole_page=True)

        assert memory.page_text(FIRST_PAGE_INDEX) == "A whole page, read better."
        assert "[joined]" not in memory.page_text(FIRST_PAGE_INDEX)

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
