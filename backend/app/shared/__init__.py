"""Shared contracts and types used across multiple modules.

This package contains models, events, and exceptions that multiple modules depend on,
preventing circular imports while keeping ownership clear:

- Reading Engine owns session state and the pointer
- Reading Speed computes predictions and analytics only
- Audio Engine consumes the pointer and plays speech only
- AI Engine consumes analytics and generates summaries
- OCR/Memory Merge remain the only source of textual content
"""

from backend.app.shared.constants import (
    FIRST_PAGE_INDEX,
    LIVE_REFRESH_MS,
    SESSION_IDLE_AFTER_MS,
    UNVERSIONED_CONTENT,
)
from backend.app.shared.events import SessionEvent
from backend.app.shared.exceptions import (
    SessionNotFoundError,
    StaleContentError,
    TaleTraceError,
)
from backend.app.shared.merge_memory import MergeMemorySource
from backend.app.shared.session_state import ReadingSessionState

__all__ = [
    "SessionEvent",
    "TaleTraceError",
    "SessionNotFoundError",
    "StaleContentError",
    "ReadingSessionState",
    "MergeMemorySource",
    "FIRST_PAGE_INDEX",
    "LIVE_REFRESH_MS",
    "SESSION_IDLE_AFTER_MS",
    "UNVERSIONED_CONTENT",
]
