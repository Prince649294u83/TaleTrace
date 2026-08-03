"""Provider-neutral Gesture Engine contracts.

`FingerPoint` is re-exported from `selection_models` rather than defined here.
This module used to declare its own — a normalized (x, y) with no confidence and
no direction — while the detector produced a different one with both. Two types
of the same name in one module is exactly the duplication the integration is
meant to remove, and the placeholder version could not have carried a real
detection anyway: a fingertip without a confidence cannot be told apart from a
guess.
"""

from pydantic import BaseModel, Field

from backend.app.models import Frame, Gesture, OCRPage, OCRWord
from backend.app.modules.gesture_engine.selection_models import FingerPoint

__all__ = [
    "FingerPoint",
    "GestureDetectionRequest",
    "GesturePlaceholderResponse",
    "GestureSelectRequest",
    "GestureSelectResponse",
    "OcrMappingRequest",
]


class GestureDetectionRequest(BaseModel):
    """Input reference for future hand-gesture detection."""

    frame: Frame


class OcrMappingRequest(BaseModel):
    """Finger coordinate and OCR content used for future word mapping."""

    finger_point: FingerPoint
    pages: list[OCRPage] = Field(default_factory=list)


class GesturePlaceholderResponse(BaseModel):
    """Stable response returned before gesture logic exists."""

    status: str = "pending"
    operation: str = "gesture_engine"
    detected: bool | None = None
    gesture: Gesture | None = None
    selected_word: OCRWord | None = None


class GestureSelectRequest(BaseModel):
    """Compatibility request for the existing gesture route."""

    selection_reference: str | None = None


class GestureSelectResponse(GesturePlaceholderResponse):
    """Compatibility response for the existing gesture route."""
