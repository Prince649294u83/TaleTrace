"""Static Gesture Engine placeholder functions; no detection logic."""

from backend.app.modules.gesture_engine.models import (
    GestureDetectionRequest,
    GesturePlaceholderResponse,
    OcrMappingRequest,
)


def detect_thumbs_up(_request: GestureDetectionRequest) -> GesturePlaceholderResponse:
    """Return the thumbs-up placeholder response."""
    return GesturePlaceholderResponse(operation="detect_thumbs_up")


def detect_pointing(_request: GestureDetectionRequest) -> GesturePlaceholderResponse:
    """Return the pointing placeholder response."""
    return GesturePlaceholderResponse(operation="detect_pointing")


def find_selected_word(_request: OcrMappingRequest) -> GesturePlaceholderResponse:
    """Return the selected-word placeholder response."""
    return GesturePlaceholderResponse(operation="find_selected_word")


def map_finger_to_ocr(_request: OcrMappingRequest) -> GesturePlaceholderResponse:
    """Return the finger-to-OCR placeholder response."""
    return GesturePlaceholderResponse(operation="map_finger_to_ocr")
