"""Gesture Engine module boundary.

The real pipeline is exported alongside the placeholder API surface. The
placeholders stay for now because `/gesture/select` still answers with them and
the frontend reads that shape; they will go when the route is wired to
`GesturePipeline`. Nothing new should import them.
"""

from backend.app.modules.gesture_engine.detector import (
    detect_finger,
    detect_finger_contour_fallback,
    detect_finger_mediapipe,
)
from backend.app.modules.gesture_engine.interfaces import GestureEngineInterface
from backend.app.modules.gesture_engine.models import (
    GestureDetectionRequest,
    GesturePlaceholderResponse,
    OcrMappingRequest,
)
from backend.app.modules.gesture_engine.pipeline import (
    API_VERSION,
    GesturePipeline,
    select_word,
)
from backend.app.modules.gesture_engine.placeholders import (
    detect_pointing,
    detect_thumbs_up,
    find_selected_word,
    map_finger_to_ocr,
)
from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
    SelectionStrategy,
)
from backend.app.modules.gesture_engine.selector import select_intended_word
from backend.app.modules.gesture_engine.visualizer import draw_overlay

__all__ = [
    "API_VERSION",
    "FingerPoint",
    "GestureDetectionRequest",
    "GestureEngineInterface",
    "GesturePipeline",
    "GesturePlaceholderResponse",
    "OcrMappingRequest",
    "SelectionConfig",
    "SelectionResult",
    "SelectionStatus",
    "SelectionStrategy",
    "detect_finger",
    "detect_finger_contour_fallback",
    "detect_finger_mediapipe",
    "detect_pointing",
    "detect_thumbs_up",
    "draw_overlay",
    "find_selected_word",
    "map_finger_to_ocr",
    "select_intended_word",
    "select_word",
]
