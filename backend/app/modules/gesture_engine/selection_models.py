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

from pydantic import BaseModel, ConfigDict, Field

from backend.app.modules.ocr.models import RecognizedWord


class SelectionStatus(str, Enum):
    """Why a selection did or did not produce a word.

    The failure cases are distinct on purpose: `NO_FINGER` means the reader is
    not pointing, `LOW_CONFIDENCE` means they are but the system will not guess,
    and `NO_WORD_FOUND` means they pointed somewhere with no text. The runtime
    responds differently to each — only the middle one is worth telling the
    reader about.
    """

    SUCCESS = "success"
    NO_FINGER = "no_finger_detected"
    LOW_CONFIDENCE = "low_selection_confidence"
    NO_WORD_FOUND = "no_word_in_search_region"
    OCR_EMPTY = "ocr_data_empty"


class SelectionStrategy(str, Enum):
    """How to interpret where the finger is.

    TOUCH suits a reader resting a fingertip on the word; POINT suits one
    gesturing at it from below. AUTO picks per frame based on whether MediaPipe
    gave a usable pointing direction, which is the only one that behaves for
    both readers without being told which they are.
    """

    STATIC_BOX = "static_box"
    DIRECTION_CONE = "direction_cone"
    AUTO = "auto"
    TOUCH = "touch"
    POINT = "point"
    HYBRID = "hybrid"


class FingerPoint(BaseModel):
    """Where the fingertip is, and how much to trust it.

    `direction` is the pointing unit vector, present only when MediaPipe resolved
    the finger joints. `None` means the contour fallback ran and the system knows
    where the finger is but not where it points — the selector widens its search
    rather than assuming straight up is right.
    """

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
    """Map a point between two frames of reference.

    The gesture frame and the OCR frame are not always the same image. The ESP32
    streams one resolution, MediaPipe may run on a downscaled copy, and the OCR
    provider may have been given a third. A fingertip found in one coordinate
    space and scored against boxes from another is off by a scale factor, which
    reads as the reader pointing at the wrong line rather than as an error.

    Identity by default, so a caller that has only one frame pays nothing.
    """

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
    # Asymmetric because a reader points *at* a word from below it: far more of
    # the useful region is above the fingertip than below.
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
    # Below this MediaPipe score the contour fallback is also run and the better
    # of the two is taken.
    fallback_trigger: float = 0.75

    use_direction: bool = True


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
    """What the reader pointed at, with the context the AI Engine needs.

    Carries `context` (the containing sentence) as well as the word, because a
    word explanation without its sentence is guesswork — "bank" cannot be defined
    without knowing whether the page is about rivers or money.
    """

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

    image_size: tuple[int, int] = (0, 0)
    selection_time_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status is SelectionStatus.SUCCESS
