"""Gesture Engine protocols without a MediaPipe dependency."""

from typing import Protocol

from backend.app.modules.gesture_engine.models import (
    GestureDetectionRequest,
    GesturePlaceholderResponse,
    OcrMappingRequest,
)


class GestureEngineInterface(Protocol):
    """Port for future hand detection and OCR coordinate mapping."""

    def detect_thumbs_up(self, request: GestureDetectionRequest) -> GesturePlaceholderResponse:
        """Detect a future thumbs-up gesture."""
        ...

    def detect_pointing(self, request: GestureDetectionRequest) -> GesturePlaceholderResponse:
        """Detect a future pointing gesture."""
        ...

    def find_selected_word(self, request: OcrMappingRequest) -> GesturePlaceholderResponse:
        """Find a future selected OCR word."""
        ...

    def map_finger_to_ocr(self, request: OcrMappingRequest) -> GesturePlaceholderResponse:
        """Map a future finger coordinate to OCR content."""
        ...
