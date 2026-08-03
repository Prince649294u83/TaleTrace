"""The one shared session state. Owned by the Reading Engine; read by everyone.

This is the single source of truth for *where the reader is and what they are
doing*. The Reading Engine writes it. The Audio Engine, Reading Speed, AI Engine
and Gesture Engine only ever read it.

That asymmetry is the whole point. Before this existed, each module kept its own
copy of the pointer and its own idea of whether the session was paused, and every
new module meant another pair of copies to keep in step. One writer removes the
synchronisation problem rather than managing it.

Deliberately narrow: only facts every module needs. Module-specific state stays in
the module that owns it — the audio queue in the Audio Engine, per-page
observations in Reading Speed. If a field here would only ever be read by one
module, it belongs in that module instead.

Frozen, because a state object that can be mutated in place is one that *will* be
mutated by a reader that only meant to look at it. The Reading Engine publishes a
new instance; readers cannot write through the copy they hold.
"""

from pydantic import BaseModel, ConfigDict, Field

# ReadingPointer lives in audio_engine.models for now. It is genuinely shared —
# Gesture moves it, the Reading Engine owns it, Audio and Reading Speed read it —
# so it belongs here, and moving it is the next sensible refactor. It is left in
# place for the moment because twenty files import it from there, and a rename
# touching all of them is a change worth making on its own rather than folded
# into this one.
from backend.app.modules.audio_engine.models import ReadingPointer


class ReadingSessionState(BaseModel):
    """Where the reader is and what they are doing, right now.

    Carries no derived numbers. Pace, deviation, difficulty and predictions are
    all computed by Reading Speed *from* this state; storing them here would
    create a second source of truth that could disagree with the first.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    reader_id: str
    book_id: str | None = None

    # Where the reader is. The pointer is the only position field: page and
    # sentence are read off it rather than stored beside it, so they cannot
    # drift apart from it.
    pointer: ReadingPointer = Field(default_factory=ReadingPointer)

    # What the reader is doing. Three flags rather than one enum because they are
    # not mutually exclusive: a reader in Meaning Mode is also not reading, and
    # collapsing that into a single value loses the distinction Reading Speed
    # depends on — a plain pause says nothing about the text, Meaning Mode says
    # the reader hit something they could not read past.
    is_reading: bool = False
    is_paused: bool = False
    is_meaning_mode: bool = False
    is_finished: bool = False

    # Whether the Audio Engine is narrating. Read by Reading Speed at session end,
    # because a session read aloud and a session read silently are not comparable
    # measurements of the same reader.
    tts_enabled: bool = False

    # Whether the camera is active. Gates Gesture detection and OCR frame delivery:
    # when off, neither module reports events. A session read from a pre-scanned
    # book with the camera off is a real use case, not degraded mode.
    camera_active: bool = True

    # Merge Memory's version for the current page. Consumers reject anything older
    # than what they already hold.
    content_version: int = 0

    @property
    def page_index(self) -> int:
        return self.pointer.page_index

    @property
    def sentence_index(self) -> int:
        return self.pointer.sentence_index

    @property
    def is_clock_running(self) -> bool:
        """Whether the reading clock should be advancing.

        The condition every consumer would otherwise reimplement, each with its
        own idea of whether Meaning Mode counts as reading time. It does not.
        """

        return self.is_reading and not (
            self.is_paused or self.is_meaning_mode or self.is_finished
        )
