"""Shared exception types for TaleTrace modules.

Base exceptions that multiple modules raise or catch. Module-specific errors (like
CalibrationError in reading_speed) stay in their own modules; only the ones that
cross boundaries live here.
"""


class TaleTraceError(Exception):
    """Base exception for all TaleTrace errors.

    Catching this catches every application error without catching built-in Python
    exceptions like KeyError or ValueError, which usually signal bugs rather than
    expected error conditions.
    """


class SessionNotFoundError(TaleTraceError, KeyError):
    """A session ID was requested that never started or has been closed.

    Inherits from KeyError so existing `except KeyError` handlers still work, but
    also from TaleTraceError so a caller can catch all application errors at once.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class StaleContentError(TaleTraceError):
    """A content update was rejected because its version is older than the current one.

    Not an error condition — OCR frames arrive over HTTP and can overtake each other,
    so rejection is a normal part of the protocol. Raised rather than returned as a
    boolean so a caller who does not expect stale frames sees the condition rather
    than silently applying nothing.
    """

    def __init__(self, current_version: int, received_version: int) -> None:
        self.current_version = current_version
        self.received_version = received_version
        super().__init__(
            f"Stale content rejected: received v{received_version}, "
            f"current is v{current_version}"
        )
