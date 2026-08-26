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


class Reader(TimestampMixin, Base):
    """What the engine needs to know about the person reading. One row, for now.

    No name, no email, no password. Those belong to whatever performs
    authentication, and nothing here does: the website's login screen is a
    `localStorage` gate for the look of the thing, and no credential ever reaches
    this server. Keeping identity out of this table means there is no second copy
    of it to disagree with the first, and no column that looks like an account
    when there are no accounts.

    What is here is the reading profile — the settings that change how a session
    is measured or played back. `device_prefs` (text-to-speech, ambient music,
    read-out-meaning) is not yet consulted by the runtime; this is where it lands
    when it is, which is why it is stored server-side rather than left in the
    browser with the rest of the cosmetics.

    `id` is a stable string, not a generated UUID, because `ReadingSpeedService`
    keys baselines by reader id and the rig passes one in from
    `recording.READER_ID`. A fresh id per row would orphan the calibration.
    """

    __tablename__ = "readers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The reader's own pace — a whole serialised `ReadingBaseline`, not just the
    # number. `is_evidence` is derived from `calibrated` and `method`, and
    # analytics refuses to infer difficulty from a baseline that is only an
    # assumption; storing the wpm alone would restart every session claiming a
    # measured baseline it never measured.
    #
    # Duplicated from `ReadingSpeedService._baselines` on purpose: that store is
    # in memory and dies with the process, so without this column a reader
    # recalibrates every time the server restarts. This row is the durable copy;
    # the service is the working one, hydrated from here at startup.
    baseline_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reading_speed_preset: Mapped[str | None] = mapped_column(String(16))

    reader_type: Mapped[str | None] = mapped_column(String(32))
    device_prefs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    theme: Mapped[str] = mapped_column(String(16), default="dark")


class Folder(TimestampMixin, Base):
    """A single-level grouping of sessions. No nesting, per the frontend spec."""

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))


class Session(TimestampMixin, Base):
    """A TaleTrace reading session.

    The measured columns are a flattened copy of `SessionAnalytics`, not a second
    calculation of it: every one is assigned straight from the value
    `ReadingEngine.finish_session` returned. Flattened rather than stored as JSON
    because the dashboard sums `pages_read` across today's rows, and summing
    inside a JSON blob means loading every session to add up two integers.

    `review_payload` is the opposite case and is stored whole. It is one AI
    response — flashcards, quiz, words learned and summary from a single call —
    and the shape the Quizzes and Flashcards pages will want from it is not
    settled yet. Splitting it across the `quizzes` and `flashcards` tables now
    would mean deciding that shape before anything reads it, and `words_learned`
    has no table at all. Those two tables are left as they were found.

    `focus_payload` is stored for a different reason again: unlike the review, a
    `FocusReport` can never be regenerated. It is derived from per-paragraph
    timings that exist only in the live engine's memory, so a session finished
    without saving it has lost that analysis permanently.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_reference: Mapped[str | None] = mapped_column(String(2048))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    name: Mapped[str] = mapped_column(String(255), default="Reading Session")
    folder_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), index=True
    )

    # --- measured, copied from SessionAnalytics ---
    baseline_wpm: Mapped[float] = mapped_column(Float, default=0.0)
    session_wpm: Mapped[float] = mapped_column(Float, default=0.0)
    words_read: Mapped[int] = mapped_column(Integer, default=0)
    pages_read: Mapped[int] = mapped_column(Integer, default=0)
    reading_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    wall_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    lookup_count: Mapped[int] = mapped_column(Integer, default=0)
    meaning_requests: Mapped[int] = mapped_column(Integer, default=0)

    # --- concluded ---
    # Stored in the backend's own vocabulary (low/medium/high/unknown) and
    # translated at the HTTP edge. Storing the website's words would put a
    # presentation choice in the database and make `unknown` unrepresentable.
    difficulty: Mapped[str] = mapped_column(String(16), default="unknown")
    summary: Mapped[str | None] = mapped_column(Text)
    review_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    focus_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lookups: Mapped[list[str]] = mapped_column(JSON, default=list)

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
