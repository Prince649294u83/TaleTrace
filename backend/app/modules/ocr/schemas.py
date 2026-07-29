"""Compatibility exports for OCR API schemas."""

from backend.app.modules.ocr.models import (
    OcrProcessRequest,
    OcrProcessResponse,
    ProcessedImage,
)

__all__ = ["OcrProcessRequest", "OcrProcessResponse", "ProcessedImage"]