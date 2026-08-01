"""Request and response schemas for the Reading Audio Engine API."""

from pydantic import BaseModel, Field

from backend.app.modules.audio_engine.models import (
    AudioProfile,
    PauseReason,
    PlaybackState,
    PlaybackStatus,
    ReadingPointer,
    Voice,
)


class StartPlaybackRequest(BaseModel):
    """Begin playback from a pointer.

    `text` is the clean paragraph from Merge Memory. `profile` accepts either a
    profile name or an AI Engine reading mode; unknown values fall back to Normal.
    """

    pointer: ReadingPointer = Field(default_factory=ReadingPointer)
    text: str
    profile: str | None = None
    voice_id: str | None = None


class PausePlaybackRequest(BaseModel):
    """Suspend playback. Meaning Mode also freezes the reading timer."""

    reason: PauseReason = PauseReason.USER


class SeekRequest(BaseModel):
    """Jump to a new pointer (Reading Update).

    Omit `text` to keep the queued sentences and only move the pointer.
    """

    pointer: ReadingPointer
    text: str | None = None


class RefreshQueueRequest(BaseModel):
    """Reload pending sentences after a Merge Memory update.

    `source_version` is Merge Memory's version for this text. Supplying it lets
    the engine reject an OCR frame that arrives after a newer one — over HTTP,
    frames can be reordered, and applying the older one would regress the text
    the reader is about to hear. Omit it and every refresh is applied.
    """

    text: str
    source_version: int | None = Field(default=None, ge=1)


class ProfileRequest(BaseModel):
    """Switch delivery profile; applies from the next sentence."""

    profile: str


class VoiceRequest(BaseModel):
    """Switch voice; applies from the next sentence."""

    voice_id: str | None = None


class PlaybackStateResponse(BaseModel):
    """Returned by every state-changing route."""

    state: PlaybackState
    session_id: str | None = None
    pointer: ReadingPointer | None = None
    profile_name: str | None = None
    queue_version: int = 0
    # False when a refresh was rejected as stale, or a closed session did not
    # exist. None where the notion does not apply.
    applied: bool | None = None


class SessionListResponse(BaseModel):
    """Every live playback session."""

    count: int = 0
    sessions: list[PlaybackStatus] = Field(default_factory=list)


class VoiceListResponse(BaseModel):
    """Voices offered by the active provider."""

    provider: str
    voices: list[Voice] = Field(default_factory=list)


class ProfileListResponse(BaseModel):
    """Every configured delivery profile."""

    profiles: list[AudioProfile] = Field(default_factory=list)


__all__ = [
    "PausePlaybackRequest",
    "PlaybackStateResponse",
    "PlaybackStatus",
    "ProfileListResponse",
    "ProfileRequest",
    "RefreshQueueRequest",
    "SeekRequest",
    "SessionListResponse",
    "StartPlaybackRequest",
    "VoiceListResponse",
    "VoiceRequest",
]
