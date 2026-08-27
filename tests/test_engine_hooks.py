"""Tests for PlaybackEngine ambient hooks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.modules.audio_engine.models import PlaybackState, ReadingPointer, SceneDecision
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine


@pytest.fixture
def mock_ambient():
    ambient = AsyncMock()
    return ambient


@pytest.fixture
def mock_scene():
    scene = AsyncMock()
    scene.evaluate.return_value = SceneDecision(
        scene_mood="test", emotion="test", intensity=0.5, audio_tag="test.mp3"
    )
    return scene


@pytest.fixture
def engine(mock_ambient, mock_scene):
    eng = PlaybackEngine(
        ambient_provider=mock_ambient,
        scene_controller=mock_scene,
        auto_advance=False,
    )
    # mock the sink to not actually play
    eng._sink = AsyncMock()
    eng._sink.stop = AsyncMock()
    return eng


class TestEngineAmbientHooks:
    @pytest.mark.asyncio
    async def test_start_triggers_scene_eval_and_crossfade(
        self, engine, mock_ambient, mock_scene
    ):
        ptr = ReadingPointer(page_index=1, paragraph_index=0)
        await engine.start(pointer=ptr, text="A test paragraph.")
        
        # start() fires the ambient task in the background. Yield to let it run.
        await asyncio.sleep(0.01)

        mock_scene.evaluate.assert_called_once_with(
            pointer=ptr, paragraph="A test paragraph."
        )
        mock_ambient.crossfade.assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_resumes_stops_ambient(
        self, engine, mock_ambient
    ):
        ptr = ReadingPointer(page_index=1, paragraph_index=0)
        await engine.start(pointer=ptr, text="Testing hooks.")
        
        await engine.pause()
        mock_ambient.pause.assert_called_once()
        
        await engine.resume()
        mock_ambient.resume.assert_called_once()
        
        await engine.stop()
        mock_ambient.stop.assert_called_once()
