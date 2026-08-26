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


def as_utc(moment: datetime) -> datetime:
    """Undo SQLite's loss of the timezone on a column `utc_now()` wrote.

    `utc_now()` stores an aware UTC datetime, but SQLite has no timestamp type and
    SQLAlchemy's format string carries no offset, so the value comes back *naive*.
    Anything that then calls `.timestamp()` or `.astimezone()` on it has Python
    assume the server's local zone and shift the row by the UTC offset — which
    reads on screen as sessions dated a day early, and only for readers west of
    Greenwich. Every consumer of a stored timestamp goes through here so the
    correction exists once rather than once per reader.
    """

    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class TimestampMixin:
    """Common creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Reader(TimestampMixin, Base):
    """The persisted identity and ownership record for a TaleTrace reader.

    Each reader has a name, email, and Argon2-hashed password, plus an opaque
    session token that is rotated on every login and cleared on logout. The
    token is stored in an HTTP-only cookie; the browser never sees the password
    hash and this server never sees the plaintext password after signup.

    The canonical seeded reader is ``live-reader`` — it owns the demo corpus
    written by ``seed_history.py`` and is initialised with ``demo@taletrace.app``
    during ``init_db()`` only when its password hash is empty, so a real user who
    has changed the demo password is never silently overwritten on restart.

    ``id`` is a stable string, not a generated UUID, because
    ``ReadingSpeedService`` keys baselines by reader id and the rig passes one
    in from ``recording.READER_ID``. A fresh id per row would orphan the
    calibration.
    """

    __tablename__ = "readers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    
    # Auth fields
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    profile_completed: Mapped[bool] = mapped_column(Integer, default=0) # bool stored as 0/1 in sqlite
    session_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

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
    reader_id: Mapped[str] = mapped_column(ForeignKey("readers.id", ondelete="CASCADE"), index=True, default="live-reader")


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
    reader_id: Mapped[str] = mapped_column(ForeignKey("readers.id", ondelete="CASCADE"), index=True, default="live-reader")

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
