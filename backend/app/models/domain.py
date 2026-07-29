"""Reusable, domain-neutral data contracts for TaleTrace modules."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    """Rectangular location within a frame or page."""

    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class Frame(BaseModel):
    """Reference metadata for an acquired frame."""

    id: str
    source: str | None = None
    captured_at: datetime | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    content_type: str | None = None


class OCRWord(BaseModel):
    """A recognized word and its optional spatial metadata."""

    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    bounding_box: BoundingBox | None = None


class OCRParagraph(BaseModel):
    """An ordered collection of OCR words."""

    text: str
    words: list[OCRWord] = Field(default_factory=list)
    bounding_box: BoundingBox | None = None


class OCRPage(BaseModel):
    """OCR output associated with a single page or frame."""

    page_number: int = Field(ge=1)
    paragraphs: list[OCRParagraph] = Field(default_factory=list)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class GestureType(str, Enum):
    """Supported gesture contract values without interpretation behavior."""

    SELECT = "select"
    NEXT = "next"
    PREVIOUS = "previous"
    PAUSE = "pause"
    RESUME = "resume"
    UNKNOWN = "unknown"


class Gesture(BaseModel):
    """A detected gesture contract."""

    type: GestureType
    confidence: float | None = Field(default=None, ge=0, le=1)
    position: BoundingBox | None = None
    detected_at: datetime | None = None


class ReadingStatus(str, Enum):
    """Possible reading lifecycle states."""

    IDLE = "idle"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ReadingState(BaseModel):
    """Current location and status within readable content."""

    status: ReadingStatus = ReadingStatus.IDLE
    page_number: int | None = Field(default=None, ge=1)
    paragraph_index: int | None = Field(default=None, ge=0)
    word_index: int | None = Field(default=None, ge=0)


class ExplanationRequest(BaseModel):
    """Reusable request contract for a future explanation provider."""

    text: str
    context: str | None = None
    reading_level: str | None = None


class ExplanationResponse(BaseModel):
    """Reusable response contract for a future explanation provider."""

    explanation: str
    source: str | None = None


class SessionStatus(str, Enum):
    """Possible session lifecycle states."""

    ACTIVE = "active"
    ENDED = "ended"


class Session(BaseModel):
    """Session identity and lifecycle metadata."""

    id: str
    status: SessionStatus
    started_at: datetime
    ended_at: datetime | None = None
    reading_state: ReadingState | None = None