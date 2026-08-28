"""Selection types for the gesture pipeline.

Migrated from `OCRandGESTURE/Gesture/gesture_engine/models.py`. The standalone
tree defined its own `OCRWord` here; that type is gone and the pipeline uses
`ocr.models.RecognizedWord` instead, so a word means one thing across the system.
Everything else — the strategies, the tuning weights, the result shape — is
preserved, because those numbers were arrived at against real pages.

`SelectionConfig` is frozen and its defaults are the tuned ones. A caller that
wants different behaviour builds a new config rather than mutating a shared one,
which matters because the config travels into scoring loops that run per frame.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.modules.ocr.models import RecognizedWord


class SelectionStatus(str, Enum):
    """Why a selection did or did not produce a word.

    The failure cases are distinct on purpose: `NO_FINGER` means the reader is
    not pointing, `LOW_CONFIDENCE` means they are but the system will not guess,
    and `NO_WORD_FOUND` means they pointed somewhere with no text.
    """

    SUCCESS = "success"
    NO_FINGER = "no_finger_detected"
    LOW_CONFIDENCE = "low_selection_confidence"
    NO_WORD_FOUND = "no_word_in_search_region"
    OCR_EMPTY = "ocr_data_empty"
    PAGE_CONTEXT_MISMATCH = "page_context_mismatch"
    OUT_OF_BOUNDS = "out_of_bounds"


class SelectionStrategy(str, Enum):
    """How to interpret where the finger is."""

    STATIC_BOX = "static_box"
    DIRECTION_CONE = "direction_cone"
    AUTO = "auto"
    TOUCH = "touch"
    POINT = "point"
    HYBRID = "hybrid"


class CoordinateSpace(BaseModel):
    """Geometric definition of a frame or page coordinate space."""

    model_config = ConfigDict(frozen=True)

    width: int
    height: int
    crop: tuple[int, int, int, int] | None = None
    rotation: int = 0
    mirror: bool = False


class PageContext(BaseModel):
    """Stable spatial and semantic coordinate space for a page."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    page_version: int = 1
    geometry_hash: str = ""
    coordinate_space: CoordinateSpace = Field(default_factory=lambda: CoordinateSpace(width=1024, height=768))
    page_region: tuple[int, int, int, int] = (0, 0, 1024, 768)
    gesture_region: tuple[int, int, int, int] = (0, 0, 1024, 768)
    ocr_region: tuple[int, int, int, int] = (0, 0, 1024, 768)
    ocr_words: tuple[RecognizedWord, ...] = ()
    created_at: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class FrameContext(BaseModel):
    """Single captured camera frame bound to a PageContext."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    frame_id: int
    page_id: str
    captured_at: float
    source_space: CoordinateSpace
    image: Any = None


class DetectionObservation(BaseModel):
    """Individual detector tier output."""

    model_config = ConfigDict(frozen=True)

    detector_name: str  # "mediapipe", "partial_finger", "tracker"
    x: float
    y: float
    direction: tuple[float, float] | None = None
    confidence: float = 0.0
    geometry_score: float = 0.0
    direction_confidence: float = 1.0
    is_predicted: bool = False
    timestamp: float = 0.0
    frame_id: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class FingerObservation(BaseModel):
    """Fused fingertip observation for word selection."""

    model_config = ConfigDict(frozen=True)

    frame_id: int
    page_id: str
    x: float
    y: float
    direction: tuple[float, float] | None = None
    confidence: float = 0.0
    geometry_score: float = 0.0
    primary_source: str = "mediapipe"
    supporting_sources: tuple[str, ...] = ()
    provenance: str = "mediapipe"  # "fused", "mediapipe", "partial_finger", "tracked"
    is_predicted: bool = False


class FingerPoint(BaseModel):
    """Where the fingertip is, and how much to trust it."""

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    confidence: float = Field(ge=0.0, le=1.0)
    direction: tuple[float, float] | None = None
    detection_method: str = "mediapipe"


class TextLine(BaseModel):
    """One line of recognised words, with the geometry selection needs."""

    model_config = ConfigDict(frozen=True)

    words: tuple[RecognizedWord, ...]
    y_center: float
    bbox: tuple[int, int, int, int]
    line_index: int
    paragraph_index: int = -1

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


class CoordinateTransformer(BaseModel):
    """Map a point between two frames of reference."""

    model_config = ConfigDict(frozen=True)

    scale_x: float = 1.0
    scale_y: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def transform(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.scale_x) + self.offset_x, (y * self.scale_y) + self.offset_y

    def inverse_transform(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.offset_x) / self.scale_x, (y - self.offset_y) / self.scale_y


class SelectionConfig(BaseModel):
    """Tuning for word selection. Defaults are the values tuned on real pages."""

    model_config = ConfigDict(frozen=True)

    selection_strategy: SelectionStrategy = SelectionStrategy.AUTO

    # Search region, in multiples of the average line height / word width.
    search_height_above: float = 1.5
    search_height_below: float = 0.3
    search_width_ratio: float = 3.0

    # Scoring weights, summing to 1.0 for the hybrid case.
    vertical_bias: float = 0.30
    horizontal_weight: float = 0.30
    direction_weight: float = 0.25
    overlap_weight: float = 0.15

    line_cluster_tolerance: float = 0.5
    confidence_threshold: float = 0.4
    mediapipe_confidence: float = 0.5
    fallback_trigger: float = 0.75

    use_direction: bool = True
    selection_margin: float = 0.05
    selection_ratio: float = 1.15
    hysteresis_margin: float = 0.15


class ScoredCandidate(BaseModel):
    """One word's score breakdown. Kept for the dashboard and for explaining a miss."""

    model_config = ConfigDict(frozen=True)

    word: RecognizedWord
    vertical_score: float
    horizontal_score: float
    direction_score: float
    overlap_score: float
    total_score: float
    scoring_reason: str = ""


class SelectionResult(BaseModel):
    """What the reader pointed at, with the context the AI Engine needs."""

    model_config = ConfigDict(frozen=True)

    status: SelectionStatus
    selected_word: str = ""
    selected_line: str = ""
    selected_line_words: tuple[str, ...] = ()
    selected_paragraph: str = ""
    context: str = ""
    confidence: float = 0.0
    finger_point: FingerPoint | None = None
    selected_word_bbox: tuple[int, int, int, int] | None = None
    word_index: int = -1
    line_index: int = -1
    paragraph_index: int = -1
    candidate_scores: tuple[ScoredCandidate, ...] = ()
    selection_reason: str = ""
    detector: str = "mediapipe"

    image_size: tuple[int, int] = (0, 0)
    selection_time_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status is SelectionStatus.SUCCESS

