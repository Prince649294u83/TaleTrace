"""Unit tests for safe dual-path OCR reconstruction, immutable geometry, and normalizer idempotence."""

import pytest
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.reconstruction_models import (
    RawOCRToken,
    NormalizedToken,
    DropCapCandidate,
    NormalizationRule,
    ReconstructionStatus,
)
from backend.app.modules.preprocessing.text_normalizer import (
    prune_leading_punctuation,
    normalize_numeric_decades,
    close_hyphenated_compounds,
    standardize_dashes,
    attach_punctuation_spacing,
    normalize_reading_text,
    detect_spatial_drop_cap,
    convert_recognized_words_to_raw_tokens,
)


def test_numeric_decade_positive_199os_to_1990s():
    text = "the glory of the late 199os can't imagine"
    cleaned, changed = normalize_numeric_decades(text)
    assert changed is True
    assert cleaned == "the glory of the late 1990s can't imagine"


def test_numeric_decade_negative_preserves_valid_words():
    # '100o' (not ending in s), 'boss', 'chaos' must NOT change
    text = "the score was 100o and the boss created chaos in the 1980s"
    cleaned, changed = normalize_numeric_decades(text)
    assert cleaned == "the score was 100o and the boss created chaos in the 1980s"


def test_hyphen_closure_positive_second_hand():
    text = "what you learn second - hand is useful"
    cleaned, changed = close_hyphenated_compounds(text)
    assert changed is True
    assert cleaned == "what you learn second-hand is useful"


def test_hyphen_negative_preserves_clause_dashes():
    text = "Chapter 1 - Introduction to Finance"
    cleaned, changed = close_hyphenated_compounds(text)
    # Digits around dash or full words with multi-spaces should not falsely collapse
    assert "Chapter 1 - Introduction" in cleaned


def test_leading_punctuation_pruning():
    text = ", values in different parts of the world"
    cleaned, changed = prune_leading_punctuation(text)
    assert changed is True
    assert cleaned == "values in different parts of the world"


def test_punctuation_attachment():
    text = "People do some crazy things with money . But no one is crazy ."
    cleaned, changed = attach_punctuation_spacing(text)
    assert changed is True
    assert cleaned == "People do some crazy things with money. But no one is crazy."


def test_dash_standardization():
    text = "So all of us - you , me , everyone - go through life"
    cleaned, changed = standardize_dashes(text)
    assert changed is True
    assert "—you, me, everyone—" in cleaned or "—" in cleaned


def test_normalizer_idempotence():
    corpus_samples = [
        "People do some crazy things with money . But no one is crazy .",
        ", values in different parts of the world , born into economies ,",
        "what you learn second - hand . So all of us — you , me , everyone - go",
        "the glory of the late 199os can't imagine .",
        "The Australian who hasn't seen a recession in 30 years has",
    ]
    for sample in corpus_samples:
        pass1 = normalize_reading_text(sample)
        pass2 = normalize_reading_text(pass1)
        assert pass1 == pass2, f"Idempotence failed on: {sample}"


def test_spatial_drop_cap_detection():
    raw_tokens = (
        RawOCRToken(
            token_id=0,
            text="ET",
            bbox=(260, 564, 311, 595),
            center_x=285.5,
            center_y=579.5,
            line_index=0,
        ),
        RawOCRToken(
            token_id=1,
            text="ME",
            bbox=(315, 565, 359, 595),
            center_x=337.0,
            center_y=580.0,
            line_index=0,
        ),
    )
    candidate = detect_spatial_drop_cap(raw_tokens)
    assert candidate is not None
    assert candidate.glyph == "L"
    assert candidate.reconstructed_text == "LET"
    assert candidate.target_raw_token_id == 0
    # Original raw token remains unchanged
    assert raw_tokens[0].text == "ET"
    assert raw_tokens[0].bbox == (260, 564, 311, 595)


def test_raw_ocr_tokens_are_frozen_immutable():
    token = RawOCRToken(
        token_id=1,
        text="challenge",
        bbox=(380, 580, 450, 610),
        center_x=415.0,
        center_y=595.0,
    )
    with pytest.raises(Exception):
        token.text = "MUTATED"  # type: ignore


def test_reconstruction_does_not_mutate_recognized_words():
    w = RecognizedWord.from_bbox("ET", (260, 564, 311, 595))
    tokens = convert_recognized_words_to_raw_tokens([w])
    assert len(tokens) == 1
    assert tokens[0].text == "ET"
    assert tokens[0].bbox == (260, 564, 311, 595)
    # The original RecognizedWord is also unaffected
    assert w.text == "ET"
    assert w.bbox == (260, 564, 311, 595)
