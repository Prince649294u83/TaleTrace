"""Reading Engine interfaces.

This module intentionally contains contracts only. Implementations may later
be backed by a repository, database, or another state store without changing
the callers of the Reading Engine.
"""

from datetime import datetime
from typing import Protocol

from backend.app.models import OCRPage, ReadingState, Session


class ReadingEngineInterface(Protocol):
    """Port for reading-session coordination and context access."""

    def start_session(self, *, session_id: str, started_at: datetime | None = None) -> Session:
        """Start a reading session and return its identity."""
        ...

    def end_session(self, *, session_id: str, ended_at: datetime | None = None) -> Session:
        """End a reading session and return its final session metadata."""
        ...

    def update_page(self, *, session_id: str, page_number: int) -> ReadingState:
        """Update the page cursor for a reading session."""
        ...

    def store_ocr(self, *, session_id: str, page: OCRPage) -> None:
        """Associate OCR output with a reading session."""
        ...

    def get_current_context(self, *, session_id: str) -> OCRPage | None:
        """Return the OCR page associated with the current reading context."""
        ...

    def get_current_paragraph(self, *, session_id: str) -> str | None:
        """Return the current paragraph representation, if available."""
        ...

    def store_selected_word(self, *, session_id: str, word: str) -> None:
        """Record the selected word for a reading session."""
        ...

    def get_session_summary(self, *, session_id: str) -> Session:
        """Return summary metadata for a reading session."""
        ...
