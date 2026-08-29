"""Phase 8: End-to-End Golden Corpus Gesture & Selection Stress Test Suite.

Validates:
1. Canonical meaning lookups on golden corpus images (challenge on p13, Financial on p14, That on p17).
2. Monotonic Safety on perturbation grid: inside points yield either SUCCESS on target or SAFE_REJECT, NEVER a wrong word.
3. Inter-word gap ambiguity rejection at (471.0, 595.0) on page 13.
4. Occluded fragment rejection for 'alds' on page 18.
"""

import json
from pathlib import Path
import pytest
import numpy as np

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    SelectionConfig,
    SelectionStatus,
)
from backend.app.modules.gesture_engine.selector import select_intended_word

CORPUS_DIR = Path(__file__).parent / "hardware_corpus"
CACHE_DIR = CORPUS_DIR / "expected" / "ocr_cache"


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


def test_page_13_canonical_challenge_selection():
    """Centroid of 'challenge' (bbox 359, 575, 470, 614) -> (414.5, 594.5) must select 'challenge'."""
    words = load_cached_words("page_13.jpg")
    finger = FingerPoint(x=414.5, y=594.5, confidence=0.95)
    config = SelectionConfig()
    result = select_intended_word(finger, words, config)

    assert result.status == SelectionStatus.SUCCESS
    assert result.selected_word.lower() == "challenge"
    assert result.evidence is not None
    assert result.evidence.tip_inside is True
    assert result.evidence.relative_margin >= 0.20


def test_page_13_between_words_gap_ambiguity_rejection():
    """Point at (471.0, 595.0) in the 1px gap between 'challenge' and 'for' must be safely rejected."""
    words = load_cached_words("page_13.jpg")
    finger = FingerPoint(x=471.0, y=595.0, confidence=0.95)
    config = SelectionConfig()
    result = select_intended_word(finger, words, config)

    # Must NOT succeed on 'for' or 'challenge'
    assert result.status in (
        SelectionStatus.BETWEEN_WORDS_AMBIGUITY,
        SelectionStatus.INSUFFICIENT_MARGIN,
        SelectionStatus.LOW_CONFIDENCE,
        SelectionStatus.SAFE_REJECT,
    )
    assert not result.succeeded
    assert result.evidence is not None
    assert result.evidence.relative_margin < 0.20


def test_page_13_perturbation_grid_monotonic_safety():
    """Perturbation grid around 'challenge' centroid (+-5px, +-10px, +-15px, +-20px).

    Monotonic Safety Principle:
    Every point either yields 'challenge' with SUCCESS or a SAFE_REJECT reason.
    NEVER selects a wrong word (e.g. 'for', 'can', 'The').
    """
    words = load_cached_words("page_13.jpg")
    config = SelectionConfig()
    cx, cy = 414.5, 594.5

    perturbations = [
        (0, 0),
        (-10, 0), (10, 0), (-20, 0), (20, 0), (-35, 0), (35, 0),
        (0, -5), (0, 5), (0, -10), (0, 10),
        (-10, -5), (10, 5), (-20, 5), (20, -5),
    ]

    for dx, dy in perturbations:
        fx, fy = cx + dx, cy + dy
        finger = FingerPoint(x=fx, y=fy, confidence=0.95)
        result = select_intended_word(finger, words, config)

        if result.succeeded:
            assert result.selected_word.lower() == "challenge", (
                f"Perturbation ({dx:+d}, {dy:+d}) at ({fx}, {fy}) selected WRONG word '{result.selected_word}'!"
            )


def test_page_14_canonical_financial_selection():
    """Meaning lookup on 'Financial' on page 14."""
    words = load_cached_words("page_14.jpg")
    financial_word = next(w for w in words if "financial" in w.text.lower())
    fx = (financial_word.bbox[0] + financial_word.bbox[2]) / 2.0
    fy = (financial_word.bbox[1] + financial_word.bbox[3]) / 2.0

    finger = FingerPoint(x=fx, y=fy, confidence=0.95)
    config = SelectionConfig()
    result = select_intended_word(finger, words, config)

    assert result.status == SelectionStatus.SUCCESS
    assert "financial" in result.selected_word.lower()
    assert result.evidence.relative_margin >= 0.20


def test_page_17_canonical_that_selection():
    """Meaning lookup on 'That' on page 17 (multi-finger hand frame)."""
    words = load_cached_words("page_17_hand.jpg")
    that_word = next(w for w in words if w.text == "That")
    fx = (that_word.bbox[0] + that_word.bbox[2]) / 2.0
    fy = (that_word.bbox[1] + that_word.bbox[3]) / 2.0

    finger = FingerPoint(x=fx, y=fy, confidence=0.95)
    config = SelectionConfig()
    result = select_intended_word(finger, words, config)

    assert result.status == SelectionStatus.SUCCESS
    assert result.selected_word == "That"


def test_page_18_occluded_fragment_alds_is_safely_rejected():
    """Touch on 'alds' occluded token on page 18 must be safely rejected as OCR_FRAGMENT_OCCLUDED."""
    words = load_cached_words("page_18.jpg")
    alds_word = next(w for w in words if w.text == "alds")
    fx = (alds_word.bbox[0] + alds_word.bbox[2]) / 2.0
    fy = (alds_word.bbox[1] + alds_word.bbox[3]) / 2.0

    finger = FingerPoint(x=fx, y=fy, confidence=0.95)
    config = SelectionConfig()
    result = select_intended_word(finger, words, config)

    assert result.status == SelectionStatus.OCR_FRAGMENT_OCCLUDED
    assert not result.succeeded
    assert result.evidence.rejection_reason == "OCR_FRAGMENT_OCCLUDED"
