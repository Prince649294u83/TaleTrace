"""OCR module request and response contracts."""

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.models import OCRPage


class ProcessedImage(BaseModel):
    """Provider-neutral reference to image data prepared by OpenCV."""

    frame_reference: str
    image_reference: str | None = None
    content: bytes | None = None
    content_type: str = "image/jpeg"


class OcrProcessRequest(BaseModel):
    """Input contract for a processed image awaiting OCR."""

    image: ProcessedImage
    detection_type: Literal["DOCUMENT_TEXT_DETECTION"] = "DOCUMENT_TEXT_DETECTION"


class OcrProcessResponse(BaseModel):
    """Normalized OCR output contract."""

    status: str = "pending"
    pages: list[OCRPage] = Field(default_factory=list)