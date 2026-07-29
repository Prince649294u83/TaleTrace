"""Placeholder OCR processor boundary.

This module deliberately does not call Google Cloud Vision or process images.
"""

from backend.app.modules.ocr.interfaces import OcrProcessorInterface

__all__ = ["OcrProcessorInterface"]