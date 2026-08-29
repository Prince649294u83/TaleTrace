"""Local Ambient Audio Provider.

Plays local .mp3 ambient loops with pygame channel crossfade.
pygame.mixer is initialized once in __init__. A missing audio device
sets _audio_enabled = False and all methods become no-ops. The reading
session continues in silence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.app.modules.audio_engine.ambient_cache import AmbientAssetCache
from backend.app.modules.audio_engine.models import SceneDecision

logger = logging.getLogger(__name__)


class LocalAmbientProvider:
    """Plays local .mp3 ambient loops with pygame channel crossfade.

    pygame.mixer is initialized once in __init__. A missing audio device
    sets _audio_enabled = False and all methods become no-ops. The reading
    session continues in silence.
    """

    def __init__(self, asset_cache: AmbientAssetCache, crossfade_ms: int = 500):
        self._cache = asset_cache
        self._crossfade_ms = crossfade_ms
        self._audio_enabled = False
        self._is_paused = False
        self._current_tag: str | None = None
        self._current_channel: Any = None
        
        # We need two channels for crossfading
        self._channels: list[Any] = []

        try:
            import pygame
            # Suppress pygame hello message if possible, though it usually
            # requires an env var before import.
            if not pygame.mixer.get_init():
                pygame.mixer.init()
                
            self._audio_enabled = pygame.mixer.get_init() is not None
            if self._audio_enabled:
                # Reserve channels for ambient so they don't clash with SFX
                pygame.mixer.set_reserved(2)
                self._channels = [pygame.mixer.Channel(0), pygame.mixer.Channel(1)]
                self._current_channel = self._channels[0]
                logger.info("pygame mixer initialized for ambient audio")
        except Exception as e:
            logger.warning("Audio device not available: %s — ambient disabled", e)
            self._audio_enabled = False

    async def crossfade(self, decision: SceneDecision, duration_ms: int = 500) -> None:
        """Fade out current track, start the new track."""
        if not self._audio_enabled:
            return

        self._is_paused = False
        if decision.audio_tag == self._current_tag:
            # Same track — adjust volume for intensity, no restart.
            if self._current_channel and self._current_channel.get_busy():
                await asyncio.to_thread(
                    self._current_channel.set_volume, decision.intensity
                )
            return

        # Different track — actual crossfade via two channels.
        path = await self._cache.resolve(decision.audio_tag)
        if path is None:
            # No asset available, stop playing and continue silently.
            await self.stop()
            self._current_tag = None
            return

        await asyncio.to_thread(
            self._crossfade_blocking, path, decision.intensity, duration_ms
        )
        self._current_tag = decision.audio_tag

    def _crossfade_blocking(self, path: Any, intensity: float, duration_ms: int) -> None:
        import pygame
        
        try:
            sound = pygame.mixer.Sound(str(path))
            sound.set_volume(intensity)
            
            # Find the other channel
            next_channel = self._channels[1] if self._current_channel == self._channels[0] else self._channels[0]
            
            # Fade out current
            if self._current_channel and self._current_channel.get_busy():
                self._current_channel.fadeout(duration_ms)
                
            # Play new
            next_channel.play(sound, loops=-1, fade_ms=duration_ms)
            self._current_channel = next_channel
            
        except Exception as e:
            logger.error("Failed to crossfade audio %s: %s", path, e)

    async def pause(self) -> None:
        """Pause ambient playback."""
        if not self._audio_enabled:
            return
        if self._current_channel:
            self._is_paused = True
            await asyncio.to_thread(self._current_channel.pause)

    async def resume(self) -> None:
        """Resume ambient playback."""
        if not self._audio_enabled:
            return
        if self._current_channel:
            self._is_paused = False
            await asyncio.to_thread(self._current_channel.unpause)

    async def stop(self) -> None:
        """Stop ambient playback entirely."""
        if not self._audio_enabled:
            return
            
        self._current_tag = None
        self._is_paused = False
        
        def _stop_all() -> None:
            for ch in self._channels:
                if ch.get_busy():
                    ch.stop()
                    
        await asyncio.to_thread(_stop_all)

    @property
    def current_tag(self) -> str | None:
        return self._current_tag

    @property
    def is_playing(self) -> bool:
        """Whether ambient audio is actively playing on a channel."""
        return bool(self._audio_enabled and not self._is_paused and self._current_channel and self._current_channel.get_busy())

    @property
    def is_paused(self) -> bool:
        """Whether ambient audio is in a paused state."""
        return bool(self._audio_enabled and self._is_paused)

    @property
    def is_enabled(self) -> bool:
        """Whether pygame mixer audio is initialized and enabled."""
        return self._audio_enabled

    @property
    def current_channel_id(self) -> int | None:
        """Active pygame channel index (0 or 1)."""
        if not self._current_channel or not self._channels:
            return None
        return 0 if self._current_channel == self._channels[0] else 1

