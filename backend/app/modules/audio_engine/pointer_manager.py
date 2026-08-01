"""Reading pointer tracking for playback.

The Reading Engine owns the canonical pointer. This module keeps the Audio
Engine's working copy and advances it as sentences complete, so playback always
knows where it is without asking anyone.
"""

from backend.app.modules.audio_engine.models import ReadingPointer


class PointerManager:
    """Implements PointerManagerInterface.

    Holds the pointer for the sentence currently being spoken. `advance()` moves
    to the next sentence; `update()` accepts an external jump (Reading Update or
    page turn).
    """

    def __init__(self, pointer: ReadingPointer | None = None) -> None:
        self._pointer = pointer

    def current_pointer(self) -> ReadingPointer:
        """Pointer at the start of the currently playing sentence.

        Defaults to the top of page 1 when nothing has been set yet, so callers
        never have to handle None.
        """

        if self._pointer is None:
            return ReadingPointer()
        return self._pointer

    def advance(self) -> ReadingPointer:
        """Move to the next sentence in the same paragraph."""

        self._pointer = self.current_pointer().next_sentence()
        return self._pointer

    def update(self, pointer: ReadingPointer) -> None:
        """Set the pointer from an external source."""

        self._pointer = pointer

    def reset(self) -> None:
        """Clear the pointer (session end)."""

        self._pointer = None

    @property
    def is_set(self) -> bool:
        """False before any pointer has been supplied."""

        return self._pointer is not None
