"""Provider-neutral Gesture Engine contracts."""

from pydantic import BaseModel, Field

from backend.app.models import Frame, Gesture, OCRPage, OCRWord


class FingerPoint(BaseModel):
    """Normalized finger coordinate independent of MediaPipe types."""

    x: float
    y: float
    normalized: bool = True


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
