"""SQLAlchemy table models for TaleTrace persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.modules.database.base import Base


def new_id() -> str:
    """Create a portable string identifier."""
    return str(uuid4())


def utc_now() -> datetime:
    """Create a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Common creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Session(TimestampMixin, Base):
    """A TaleTrace reading session."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_reference: Mapped[str | None] = mapped_column(String(2048))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ocr_results: Mapped[list[OCRResult]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    selected_words: Mapped[list[SelectedWord]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    ai_responses: Mapped[list[AIResponse]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    flashcards: Mapped[list[Flashcard]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    quizzes: Mapped[list[Quiz]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    reading_statistics: Mapped[list[ReadingStatistic]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class OCRResult(TimestampMixin, Base):
    """Normalized OCR output captured during a session."""

    __tablename__ = "ocr_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    source_reference: Mapped[str | None] = mapped_column(String(2048))
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    structured_content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="ocr_results")
    selected_words: Mapped[list[SelectedWord]] = relationship(back_populates="ocr_result")


class SelectedWord(TimestampMixin, Base):
    """An OCR word selected by a reader gesture."""

    __tablename__ = "selected_words"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    ocr_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("ocr_results.id", ondelete="SET NULL"), index=True
    )
    text: Mapped[str] = mapped_column(String(512))
    bounding_box: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="selected_words")
    ocr_result: Mapped[OCRResult | None] = relationship(back_populates="selected_words")


class AIResponse(TimestampMixin, Base):
    """A persisted response from a future AI capability."""

    __tablename__ = "ai_responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    capability: Mapped[str] = mapped_column(String(64), index=True)
    prompt_reference: Mapped[str | None] = mapped_column(String(2048))
    response_text: Mapped[str] = mapped_column(Text, default="")
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    session: Mapped[Session] = relationship(back_populates="ai_responses")


class Flashcard(TimestampMixin, Base):
    """A study flashcard associated with a reading session."""

    __tablename__ = "flashcards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    front: Mapped[str] = mapped_column(Text)
    back: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(String(2048))

    session: Mapped[Session] = relationship(back_populates="flashcards")


class Quiz(TimestampMixin, Base):
    """A generated quiz and its structured questions."""

    __tablename__ = "quizzes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    score: Mapped[float | None] = mapped_column(Float)

    session: Mapped[Session] = relationship(back_populates="quizzes")


class ReadingStatistic(TimestampMixin, Base):
    """Aggregated reading metrics for a session."""

    __tablename__ = "reading_statistics"
    __table_args__ = (
        CheckConstraint("words_read >= 0", name="ck_reading_statistics_words_read"),
        CheckConstraint("duration_seconds >= 0", name="ck_reading_statistics_duration"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    words_read: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    pages_read: Mapped[int] = mapped_column(Integer, default=0)
    selections_count: Mapped[int] = mapped_column(Integer, default=0)
    additional_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    session: Mapped[Session] = relationship(back_populates="reading_statistics")
