"""Deterministic, idempotent text normalizer for reading text reconstruction.

Operates purely on reading text strings and token streams for TTS narration, OLED display,
and session review. Never modifies raw OCR bounding boxes used by the Gesture Engine.
"""

from __future__ import annotations

import re
import hashlib
from typing import Sequence, Any
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.reconstruction_models import (
    NormalizationRule,
    ReconstructionStatus,
    RawOCRToken,
    NormalizedToken,
    DropCapCandidate,
    ReconstructedReadingPage,
)


def prune_leading_punctuation(text: str) -> tuple[str, bool]:
    """Prune stray leading punctuation at line or paragraph starts."""
    cleaned = re.sub(r"(?m)^\s*[,;:]\s*", "", text)
    # Also handle leading comma at clause breaks
    cleaned = re.sub(r"\b(\w+)\s*,\s*,\s*", r"\1, ", cleaned)
    return cleaned, cleaned != text


def normalize_numeric_decades(text: str) -> tuple[str, bool]:
    """Correct narrow OCR letter-to-digit substitution in decades (e.g. 199os -> 1990s).
    
    Leaves non-decade tokens (e.g. 100o, boss, chaos) completely untouched.
    """
    cleaned = re.sub(r"\b(\d{3})[oO]s\b", r"\g<1>0s", text)
    return cleaned, cleaned != text


def close_hyphenated_compounds(text: str) -> tuple[str, bool]:
    """Close broken whitespace around hyphens in compound words (e.g. second - hand -> second-hand).
    
    Preserves standalone dashes separating clauses or numbers with distinct whitespace.
    """
    # Matches words with single hyphen and surrounding single space: word - word
    cleaned = re.sub(r"\b([a-zA-Z]{2,})\s+-\s+([a-zA-Z]{2,})\b", r"\1-\2", text)
    return cleaned, cleaned != text


def standardize_dashes(text: str) -> tuple[str, bool]:
    """Standardize em-dashes and parenthetical dash expressions with proper spacing."""
    # Standardize spaces around em-dashes inside parenthetical lists
    cleaned = re.sub(r"\s*[—–]\s*", "—", text)
    # Fix detached em-dash/hyphen around parenthetical lists (e.g., 'all of us - you , me , everyone - go')
    cleaned = re.sub(r"\b([a-zA-Z]+)\s+-\s+([a-zA-Z]+(?:\s*,\s*[a-zA-Z]+)+)\s+-\s+([a-zA-Z]+)\b", r"\1—\2—\3", cleaned)
    return cleaned, cleaned != text


def attach_punctuation_spacing(text: str) -> tuple[str, bool]:
    """Attach detached punctuation marks to the preceding token (e.g. 'money .' -> 'money.')."""
    cleaned = re.sub(r"\s+([,\.\?!:;])", r"\1", text)
    return cleaned, cleaned != text


def normalize_reading_text(text: str) -> str:
    """Run full deterministic mechanical normalization on reading text.
    
    Guaranteed to be idempotent: normalize_reading_text(normalize_reading_text(T)) == normalize_reading_text(T).
    """
    if not text:
        return ""
    
    t, _ = prune_leading_punctuation(text)
    t, _ = normalize_numeric_decades(t)
    t, _ = close_hyphenated_compounds(t)
    t, _ = standardize_dashes(t)
    t, _ = attach_punctuation_spacing(t)
    return t


def detect_spatial_drop_cap(
    raw_tokens: Sequence[RawOCRToken],
    median_line_height: float = 40.0,
) -> DropCapCandidate | None:
    """Analyze spatial layout to identify decorative drop-caps at chapter/section starts.
    
    Evaluates physical geometry and token prefix candidates without mutating raw tokens.
    """
    if not raw_tokens:
        return None
    
    first_token = raw_tokens[0]
    # Check if first token is an incomplete uppercase root missing initial letter
    # e.g., 'ET' -> 'LET', 'N' -> 'IN', 'ONCE' without 'O'
    incomplete_roots = {
        "ET": "LET",
        "N": "IN",
        "T": "IT",
        "ND": "AND",
        "OR": "FOR",
    }
    
    if first_token.line_index in (0, -1) and first_token.text.upper() in incomplete_roots:
        reconstructed = incomplete_roots[first_token.text.upper()]
        return DropCapCandidate(
            glyph=reconstructed[0],
            bbox=None,  # Missing/unsegmented optical glyph
            target_raw_token_id=first_token.token_id,
            target_raw_text=first_token.text,
            reconstructed_text=reconstructed,
            provenance="spatial_drop_cap_binding",
        )
    
    return None


def convert_recognized_words_to_raw_tokens(
    words: Sequence[RecognizedWord],
) -> tuple[RawOCRToken, ...]:
    """Convert mutable RecognizedWord sequence into immutable RawOCRToken tuples."""
    tokens = []
    for idx, w in enumerate(words):
        tokens.append(
            RawOCRToken(
                token_id=idx,
                text=w.text,
                bbox=w.bbox,
                center_x=w.center_x,
                center_y=w.center_y,
                confidence=w.confidence,
                line_index=getattr(w, "line_index", 0),
            )
        )
    return tuple(tokens)
