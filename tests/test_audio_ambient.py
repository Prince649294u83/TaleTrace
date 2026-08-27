"""Tests for the ambient audio layer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.modules.audio_engine.ambient_cache import AmbientAssetCache
from backend.app.modules.audio_engine.local_ambient import LocalAmbientProvider
from backend.app.modules.audio_engine.models import SceneDecision


class TestAmbientAssetCache:
    @pytest.mark.asyncio
    async def test_resolves_existing_local_file(self, tmp_path: Path):
        (tmp_path / "test_ambient.mp3").touch()
        cache = AmbientAssetCache(assets_dir=tmp_path)
        
        resolved = await cache.resolve("test_ambient")
        assert resolved == tmp_path / "test_ambient.mp3"

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, tmp_path: Path):
        cache = AmbientAssetCache(assets_dir=tmp_path)
        
        resolved = await cache.resolve("missing.mp3")
        assert resolved is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_tag(self, tmp_path: Path):
        cache = AmbientAssetCache(assets_dir=tmp_path)
        assert await cache.resolve("") is None
        assert await cache.resolve(None) is None

    @pytest.mark.asyncio
    async def test_prevents_directory_traversal(self, tmp_path: Path):
        parent = tmp_path.parent
        (parent / "secret.mp3").touch()
        
        cache = AmbientAssetCache(assets_dir=tmp_path)
        resolved = await cache.resolve("../secret.mp3")
        
        assert resolved is None


import sys

class TestLocalAmbientProvider:
    @pytest.fixture
    def mock_cache(self, tmp_path: Path):
        class MockCache:
            async def resolve(self, tag: str) -> Path | None:
                if tag == "missing.mp3":
                    return None
                return tmp_path / tag
        return MockCache()

    @pytest.fixture
    def mock_pygame(self):
        mock_pg = MagicMock()
        with patch.dict(sys.modules, {"pygame": mock_pg}):
            yield mock_pg

    def test_audio_disabled_if_pygame_fails(self, mock_cache, mock_pygame):
        mock_pygame.mixer.get_init.return_value = None
        mock_pygame.mixer.init.side_effect = Exception("No device")
        provider = LocalAmbientProvider(asset_cache=mock_cache)
        assert provider._audio_enabled is False

    @pytest.mark.asyncio
    async def test_methods_no_op_when_disabled(self, mock_cache, mock_pygame):
        mock_pygame.mixer.get_init.return_value = None
        mock_pygame.mixer.init.side_effect = Exception("No device")
        provider = LocalAmbientProvider(asset_cache=mock_cache)
        
        # None of these should raise or block
        await provider.crossfade(SceneDecision(audio_tag="test.mp3", intensity=0.5))
        await provider.pause()
        await provider.resume()
        await provider.stop()

    @pytest.mark.asyncio
    async def test_crossfade_same_track_adjusts_volume_only(
        self, mock_cache, mock_pygame
    ):
        mock_pygame.mixer.get_init.return_value = True
        provider = LocalAmbientProvider(asset_cache=mock_cache)
        
        ch1 = mock_pygame.mixer.Channel.return_value
        ch1.get_busy.return_value = True
        
        provider._current_tag = "same.mp3"
        provider._current_channel = ch1

        decision = SceneDecision(audio_tag="same.mp3", intensity=0.7)
        await provider.crossfade(decision)

        # It adjusted volume, but did not create a new Sound or play
        ch1.set_volume.assert_called_once_with(0.7)
        ch1.play.assert_not_called()
        mock_pygame.mixer.Sound.assert_not_called()

    @pytest.mark.asyncio
    async def test_crossfade_different_track_crossfades(
        self, mock_cache, tmp_path, mock_pygame
    ):
        mock_pygame.mixer.get_init.return_value = True
        # We need two channels returned sequentially
        ch1, ch2 = MagicMock(), MagicMock()
        mock_pygame.mixer.Channel.side_effect = [ch1, ch2]
        
        provider = LocalAmbientProvider(asset_cache=mock_cache)
        provider._channels = [ch1, ch2]
        provider._current_channel = ch1
        ch1.get_busy.return_value = True
        
        decision = SceneDecision(audio_tag="new.mp3", intensity=0.8)
        await provider.crossfade(decision)

        # It faded out the first channel
        ch1.fadeout.assert_called_once_with(500)
        
        # It played on the second channel
        ch2.play.assert_called_once()
        assert provider.current_tag == "new.mp3"
        assert provider._current_channel == ch2

    @pytest.mark.asyncio
    async def test_missing_track_stops_current_and_falls_back_to_silence(
        self, mock_cache, mock_pygame
    ):
        mock_pygame.mixer.get_init.return_value = True
        provider = LocalAmbientProvider(asset_cache=mock_cache)
        ch1 = MagicMock()
        ch1.get_busy.return_value = True
        provider._channels = [ch1]
        provider._current_tag = "old.mp3"
        
        decision = SceneDecision(audio_tag="missing.mp3", intensity=0.5)
        await provider.crossfade(decision)

        # Stops existing because the new scene has no audio, meaning silence is required
        ch1.stop.assert_called_once()
        assert provider.current_tag is None
