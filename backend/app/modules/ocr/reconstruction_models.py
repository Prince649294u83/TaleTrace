"""Data models for safe dual-path OCR reconstruction and immutable geometry preservation.

TaleTrace maintains a strict separation between physical raw OCR observations
(used by the Gesture Engine for word selection) and derived semantic reading text
(used for TTS narration, OLED display, and session reviews).
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class NormalizationRule(str, Enum):
    """Explicit provenance tag for each transformation rule."""
    DROP_CAP_SYNTHESIS = "drop_cap_synthesis"
    LEADING_PUNCTUATION_PRUNING = "leading_punctuation_pruning"
    HYPHEN_COMPOUND_CLOSURE = "hyphen_compound_closure"
    DASH_STANDARDIZATION = "dash_standardization"
    NUMERIC_DECADE_SUBSTITUTION = "numeric_decade_substitution"
    PUNCTUATION_ATTACHMENT = "punctuation_attachment"
    CONTEXTUAL_SEMANTIC_REPAIR = "contextual_semantic_repair"
    NO_OP = "no_op"


class ReconstructionStatus(str, Enum):
    """Execution status of a normalized token or text segment."""
    EXACT_RAW = "exact_raw"
    MECHANICALLY_NORMALIZED = "mechanically_normalized"
    SEMANTICALLY_RECONSTRUCTED = "semantically_reconstructed"
    ABSTAINED_UNCERTAIN = "abstained_uncertain"


class RawOCRToken(BaseModel):
    """Immutable physical OCR observation produced directly by the optical engine.
    
    This geometry is authoritative for the Gesture Engine, coordinate transforms,
    and PageContext hashing. It is never mutated by text normalization.
    """
    model_config = ConfigDict(frozen=True)

    token_id: int
    text: str
    bbox: tuple[int, int, int, int]
    center_x: float
    center_y: float
    confidence: float = 1.0
    line_index: int = 0
    paragraph_index: int = -1


class NormalizedToken(BaseModel):
    """Derived semantic token with strict provenance linking to source raw tokens."""
    model_config = ConfigDict(frozen=True)

    output_text: str
    source_token_ids: tuple[int, ...]
    rule_applied: NormalizationRule
    status: ReconstructionStatus
    confidence_score: float = 1.0
    notes: str | None = None


class DropCapCandidate(BaseModel):
    """Spatial relationship between a decorative drop-cap and opening body text."""
    model_config = ConfigDict(frozen=True)

    glyph: str
    bbox: tuple[int, int, int, int] | None = None
    target_raw_token_id: int
    target_raw_text: str
    reconstructed_text: str
    provenance: str = "spatial_drop_cap_binding"


class ReconstructedReadingPage(BaseModel):
    """Complete reconstructed reading text for audio narration and display."""
    model_config = ConfigDict(frozen=True)

    page_id: str
    geometry_hash: str
    raw_tokens: tuple[RawOCRToken, ...]
    normalized_tokens: tuple[NormalizedToken, ...]
    reading_paragraphs: tuple[str, ...]
    provenance_log: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    reconstruction_engine: str = "deterministic_local"
