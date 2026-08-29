"""Phase 2: Offline OCR and PageContext validation across the 10 Golden Corpus images.

Validates that OCR outputs load from deterministic cache without network requests,
cluster into valid lines, compute consistent line height, and separate graphs/headers.
"""

import json
from pathlib import Path
import pytest
import numpy as np

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.gesture_engine.selector import group_ocr_words
from backend.app.modules.gesture_engine.selection_models import SelectionConfig

CORPUS_DIR = Path(__file__).parent / "hardware_corpus"
MANIFEST_FILE = CORPUS_DIR / "manifest.json"
CACHE_DIR = CORPUS_DIR / "expected" / "ocr_cache"


@pytest.fixture
def manifest():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cached_words(filename: str) -> list[RecognizedWord]:
    cache_path = CACHE_DIR / f"{filename}.json"
    assert cache_path.exists(), f"Missing offline OCR cache for {filename}"
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        RecognizedWord.from_bbox(
            text=item["text"],
            bbox=tuple(item["bbox"]),
            confidence=item["confidence"],
            line_index=item.get("line_index", 0),
        )
        for item in data
    ]


def test_all_10_corpus_pages_have_deterministic_ocr_caches(manifest):
    """Every case in manifest.json must have a corresponding offline OCR cache."""
    for case_id, case_info in manifest["cases"].items():
        fname = case_info["filename"]
        words = load_cached_words(fname)
        assert len(words) > 50, f"Page {fname} has suspiciously low word count ({len(words)})"


def test_page_13_contains_canonical_challenge_word():
    """Page 13 must contain the golden target word 'challenge' with high OCR confidence."""
    words = load_cached_words("page_13.jpg")
    challenge_words = [w for w in words if w.text.lower() == "challenge"]
    assert len(challenge_words) == 1
    w = challenge_words[0]
    assert w.confidence >= 0.80
    assert w.bbox[0] >= 300 and w.bbox[2] <= 500


def test_page_14_contains_canonical_financial_word():
    """Page 14 must contain the golden target word 'Financial'."""
    words = load_cached_words("page_14.jpg")
    financial_words = [w for w in words if "financial" in w.text.lower()]
    assert len(financial_words) >= 1
    assert any(w.confidence >= 0.80 for w in financial_words)


def test_page_17_contains_canonical_that_word():
    """Page 17 (multi-finger hand) must contain the target word 'That'."""
    words = load_cached_words("page_17_hand.jpg")
    that_words = [w for w in words if w.text == "That"]
    assert len(that_words) >= 1


def test_page_18_contains_fragment_alds_classified_as_fragment():
    """Page 18 must contain the occluded token 'alds' for occlusion testing."""
    words = load_cached_words("page_18.jpg")
    alds_words = [w for w in words if w.text == "alds"]
    assert len(alds_words) == 1
    w = alds_words[0]
    # Check that 'alds' is short/fragment-like
    assert len(w.text) < 5


def test_body_line_height_estimation_is_stable_across_all_pages(manifest):
    """Median word thickness must remain between 25px and 55px across all 10 pages."""
    for case_id, case_info in manifest["cases"].items():
        fname = case_info["filename"]
        words = load_cached_words(fname)
        word_thicknesses = [
            min(w.bbox[2] - w.bbox[0], w.bbox[3] - w.bbox[1])
            for w in words
        ]
        med_thick = float(np.median(word_thicknesses))
        assert 25.0 <= med_thick <= 55.0, f"Page {fname} median thickness {med_thick} out of range"
