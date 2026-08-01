"""Static Reading Audio Engine placeholders with no synthesis or playback.

Mirrors the pattern used by the gesture and AI engines: a capability shell that
answers with a stable, identifiable response so consumers can wire against the
boundary before the real engine is available.
"""

from backend.app.modules.audio_engine.models import (
    AudioProfile,
    PlaybackState,
    PlaybackStatus,
    ReadingPointer,
    SentenceChunk,
    SpeechRequest,
    SpeechResponse,
    Voice,
)


class PlaybackEnginePlaceholder:
    """Accepts every playback call and stays IDLE."""

    capability = "playback_engine"

    async def start(
        self,
        *,
        pointer: ReadingPointer,
        text: str,
        profile: AudioProfile | None = None,
        voice_id: str | None = None,
    ) -> PlaybackState:
        return PlaybackState.IDLE

    async def pause(self, *, reason=None) -> PlaybackState:
        return PlaybackState.IDLE

    async def resume(self) -> PlaybackState:
        return PlaybackState.IDLE

    async def stop(self) -> PlaybackState:
        return PlaybackState.IDLE

    async def seek(
        self, *, pointer: ReadingPointer, text: str | None = None
    ) -> PlaybackState:
        return PlaybackState.IDLE

    async def refresh_queue(
        self, *, text: str | None = None, sentences: list[SentenceChunk] | None = None
    ) -> None:
        return None

    def get_status(self) -> PlaybackStatus:
        return PlaybackStatus(state=PlaybackState.IDLE, provider=self.capability)


class SpeechProviderPlaceholder:
    """Returns no audio and no voices."""

    provider_name = "placeholder"

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        return SpeechResponse(provider=self.provider_name, error="No speech provider configured")

    async def get_available_voices(self) -> list[Voice]:
        return []
