"""OCR parser boundary.

The concrete provider response parser will be added when Google Cloud Vision
integration is implemented. This module intentionally has no parsing logic.
"""

from backend.app.modules.ocr.interfaces import OcrParserInterface

__all__ = ["OcrParserInterface"]