"""Database model boundary."""

from backend.app.modules.database.base import Base
from backend.app.modules.database.models import (
    AIResponse,
    Flashcard,
    Folder,
    OCRResult,
    Quiz,
    Reader,
    ReadingStatistic,
    SelectedWord,
    Session,
)
from backend.app.modules.database.session import db_session, get_db, init_db

__all__ = [
    "AIResponse",
    "Base",
    "Flashcard",
    "Folder",
    "OCRResult",
    "Quiz",
    "Reader",
    "ReadingStatistic",
    "SelectedWord",
    "Session",
    "db_session",
    "get_db",
    "init_db",
]