"""OCR module boundary."""

from backend.app.modules.ocr.interfaces import OcrParserInterface, OcrProcessorInterface
from backend.app.modules.ocr.models import OcrProcessRequest, OcrProcessResponse, ProcessedImage

__all__ = [
    "OcrParserInterface",
    "OcrProcessorInterface",
    "OcrProcessRequest",
    "OcrProcessResponse",
    "ProcessedImage",
]