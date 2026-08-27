"""The OCR.Space spatial chain: image → words → fingertip → selected word → sentence.

This test replays a recorded OCR.Space response (not a live API call) through
the same parsing and gesture-selection code production uses. It locks in the
result that validated OCR.Space as a prototype provider:

    same image + same OCR result + same fingertip = same word = same sentence

Changes to the OCR parser, the gesture selector, or the scoring weights that
would silently break word selection on real pages are caught here.
"""

from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pytest

from backend.app.modules.gesture_engine.pipeline import GesturePipeline
from backend.app.modules.gesture_engine.selection_models import FingerPoint
from backend.app.modules.ocr.ocr_space import OcrSpaceProvider
from backend.app.modules.reading_engine.runtime import ReadingRuntime

# We don't need a real image for the gesture selector, just the dimensions
# to normalize coordinates. The image from the test was 1280x960.
IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 960
DUMMY_FRAME = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH, 3), dtype=np.uint8)


def _load_fixture() -> dict:
    path = Path(__file__).parent / "fixtures" / "ocr_space_pointing_page.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def test_ocr_space_center_hit_selects_expected_word():
    # 1. Parse the recorded JSON using the production parser
    fixture = _load_fixture()
    words = OcrSpaceProvider.parse_response(fixture)
    
    # Basic sanity check that parsing worked
    assert len(words) > 0
    assert len(words) == 272  # The known count for this fixture
    
    # 2. Select using the exact fingertip coordinate from the real test
    gesture = GesturePipeline()
    finger = FingerPoint(x=577, y=796, confidence=0.99)
    
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=finger)
    
    assert selection.succeeded
    assert selection.selected_word == "impact"


def test_sentence_grounding_finds_the_containing_sentence():
    # Proves the sentence extraction logic works on the lines OCR.Space returns.
    fixture = _load_fixture()
    words = OcrSpaceProvider.parse_response(fixture)
    
    gesture = GesturePipeline()
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=FingerPoint(x=577, y=796, confidence=0.99))
    
    class DummyProvider:
        provider_name = "ocr_space_test"
        def accepts(self, source): return True
        def extract(self, source): return words
        
    runtime = ReadingRuntime.build(
        session_id="regression",
        reader_id="regression",
        ocr_provider=DummyProvider(),
        focus=False
    )
    
    # Push the words into memory as if they came from the camera
    import asyncio
    asyncio.run(runtime.engine.ingest_frame(b"dummy"))
    
    # Find the sentence containing the selected line
    loc = runtime._locate_line(selection.selected_line)
    assert loc is not None
    
    paragraph_idx, sentence_idx = loc
    page_idx = runtime.engine.state.pointer.page_index
    paragraph = runtime.engine.memory.paragraph(page_idx, paragraph_idx)
    
    from backend.app.modules.audio_engine.sentence_queue import segment_sentences
    chunks = list(segment_sentences(paragraph))

    # OCR.Space fragmentation causes the sentence to be split across multiple chunks
    full_text = " ".join(chunk.text for chunk in chunks)
    assert "Two topics impact everyone" in full_text


def test_pointing_at_topics_selects_topics():
    words = OcrSpaceProvider.parse_response(_load_fixture())
    gesture = GesturePipeline()
    finger = FingerPoint(x=494, y=778, confidence=0.99)
    
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=finger)
    assert selection.succeeded
    assert selection.selected_word == "topics"


def test_pointing_at_everyone_selects_everyone():
    words = OcrSpaceProvider.parse_response(_load_fixture())
    gesture = GesturePipeline()
    finger = FingerPoint(x=711, y=810, confidence=0.99)
    
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=finger)
    assert selection.succeeded
    assert selection.selected_word == "everyone"


def test_pointing_at_when_selects_when():
    words = OcrSpaceProvider.parse_response(_load_fixture())
    gesture = GesturePipeline()
    finger = FingerPoint(x=581, y=651, confidence=0.99)
    
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=finger)
    assert selection.succeeded
    assert selection.selected_word == "when"


def test_pointing_at_money_selects_money():
    words = OcrSpaceProvider.parse_response(_load_fixture())
    gesture = GesturePipeline()
    finger = FingerPoint(x=682, y=857, confidence=0.99)
    
    selection = gesture.process_frame(DUMMY_FRAME, words, finger=finger)
    assert selection.succeeded
    assert selection.selected_word == "money"
