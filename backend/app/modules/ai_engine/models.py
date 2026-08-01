"""Provider-neutral AI Engine request and placeholder response models."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReadingMode(str, Enum):
    """Reading modes the AI Engine can adapt its tone and depth to."""

    STANDARD = "standard"
    ADAPTIVE = "adaptive"
    DISABILITY = "disability"
    NOVEL = "novel"
    STUDY = "study"
    EXAM = "exam"


class DifficultyLevel(str, Enum):
    """How demanding an explanation is; consumed by Disability/Adaptive modes."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ImageType(str, Enum):
    """Style of supporting image, so retrieval can pick the right source."""

    ILLUSTRATION = "illustration"
    DIAGRAM = "diagram"
    PHOTO = "photo"
    MAP = "map"
    PORTRAIT = "portrait"
    NONE = "none"


class BookMetadata(BaseModel):
    """Identity of the book being read. Keeps prompts book-agnostic."""

    title: str | None = None
    author: str | None = None
    genre: str | None = None
    chapter: str | None = None
    audience: str | None = None


class LearnedWord(BaseModel):
    """A word already explained earlier in the session."""

    model_config = ConfigDict(extra="allow")

    word: str
    definition: str | None = None

    def recap(self) -> str:
        """Best available short meaning, tolerating older payload key names."""

        extras = self.model_extra or {}
        meaning = (
            self.definition
            or extras.get("fun_definition")
            or extras.get("back")
            or extras.get("full_explanation")
            or ""
        )
        return f"'{self.word}' meaning {meaning}" if meaning else f"'{self.word}'"


class LookupRecord(BaseModel):
    """One word lookup recorded during a reading session."""

    model_config = ConfigDict(extra="allow")

    word: str
    context: str | None = None
    mode_used: str | None = None
    page_number: int | None = None


class ReadingContext(BaseModel):
    """Full reading state handed to the AI Engine by the Reading Engine.

    Every field is optional so callers can send as much context as they have.
    """

    book: BookMetadata = Field(default_factory=BookMetadata)
    page_number: int | None = None
    previous_paragraph: str | None = None
    current_paragraph: str | None = None
    selected_word: str | None = None
    mode: ReadingMode = ReadingMode.STANDARD
    previously_explained: list[LearnedWord] = Field(default_factory=list)


class AiInput(BaseModel):
    """Context supplied to an AI capability.

    `context` is the preferred input. `text` and `metadata` remain supported so
    existing callers keep working; `resolved_context()` merges both.
    """

    content_reference: str | None = None
    text: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    context: ReadingContext | None = None

    def resolved_context(self) -> ReadingContext:
        """Return the reading context, filling gaps from legacy flat fields."""

        context = self.context.model_copy(deep=True) if self.context else ReadingContext()

        if context.current_paragraph is None:
            context.current_paragraph = self.text
        if context.selected_word is None and self.metadata.get("word"):
            context.selected_word = self.metadata["word"]
        if self.context is None and self.metadata.get("mode"):
            try:
                context.mode = ReadingMode(self.metadata["mode"])
            except ValueError:
                context.mode = ReadingMode.STANDARD

        book = context.book
        if book.title is None and self.metadata.get("book_title"):
            book.title = self.metadata["book_title"]
        if book.genre is None and self.metadata.get("genre"):
            book.genre = self.metadata["genre"]
        if book.chapter is None and self.metadata.get("chapter"):
            book.chapter = self.metadata["chapter"]
        if book.author is None and self.metadata.get("author"):
            book.author = self.metadata["author"]
        if book.audience is None and self.metadata.get("audience"):
            book.audience = self.metadata["audience"]

        return context


class AiPlaceholderResponse(BaseModel):
    """Stable response returned until a capability is implemented."""

    status: str = "pending"
    capability: str = "ai_engine"
    message: str = "AI capability placeholder"


class AiCapabilityResponse(AiPlaceholderResponse):
    """Response carrying a parsed capability payload.

    `data` is the structured result consumers should read. `message` keeps the
    same JSON string earlier callers relied on.
    """

    data: dict[str, Any] = Field(default_factory=dict)


class AiExplainRequest(AiInput):
    """Input contract for the explanation route."""

    previously_explained: list[LearnedWord] = Field(default_factory=list)

    def resolved_context(self) -> ReadingContext:
        """Merge the route-level `previously_explained` list into the context."""

        context = super().resolved_context()
        if self.previously_explained and not context.previously_explained:
            context.previously_explained = self.previously_explained
        return context


class AiExplainResponse(AiCapabilityResponse):
    """Response for the explanation route."""


class AiSessionSummaryRequest(AiInput):
    """Input contract for session-end flashcard/quiz/summary generation."""

    session_history: list[LookupRecord] = Field(default_factory=list)
