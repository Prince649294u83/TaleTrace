"""Database model boundary."""

from backend.app.modules.database.base import Base
from backend.app.modules.database.models import (
    AIResponse,
    Flashcard,
    OCRResult,
    Quiz,
    ReadingStatistic,
    SelectedWord,
    Session,
)

__all__ = [
    "AIResponse",
    "Base",
    "Flashcard",
    "OCRResult",
    "Quiz",
    "ReadingStatistic",
    "SelectedWord",
    "Session",
]