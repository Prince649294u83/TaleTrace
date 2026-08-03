"""Shared API and domain-neutral models."""

from backend.app.models.domain import (
    BoundingBox,
    ExplanationRequest,
    ExplanationResponse,
    Frame,
    Gesture,
    GestureType,
    OCRPage,
    OCRParagraph,
    OCRWord,
    ReadingState,
    ReadingStatus,
    Session,
    SessionStatus,
)
from backend.app.models.responses import ResponseEnvelope

__all__ = [
    "BoundingBox",
    "ExplanationRequest",
    "ExplanationResponse",
    "Frame",
    "Gesture",
    "GestureType",
    "OCRPage",
    "OCRParagraph",
    "OCRWord",
    "ReadingState",
    "ReadingStatus",
    "ResponseEnvelope",
    "Session",
    "SessionStatus",
]