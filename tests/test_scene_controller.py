"""Tests for the SceneController.

Validates the caching, concurrent deduplication, and failure boundaries
between the audio engine and the AI scene evaluation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.app.modules.audio_engine.models import PlaybackState, ReadingPointer, SceneDecision
from backend.app.modules.audio_engine.scene_controller import SceneController, _DEFAULT_SCENE


@dataclass
class FakeOutcome:
    ok: bool
    data: dict[str, Any]
    error: str = ""


@dataclass
class FakeAiBridge:
    """Records calls and returns controlled outcomes for scene evaluation."""

    calls: list[tuple[str, int]] = field(default_factory=list)
    outcomes: list[FakeOutcome] = field(default_factory=list)
    delay_seconds: float = 0.0

    async def scene_mood(self, *, paragraph: str, page_number: int) -> FakeOutcome:
        self.calls.append((paragraph, page_number))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        
        if self.outcomes:
            return self.outcomes.pop(0)
            
        return FakeOutcome(
            ok=True,
            data={
                "scene_mood": "suspense",
                "emotion": "fear",
                "intensity": 0.8,
                "audio_tag": "sfx_dark_wind.mp3",
            }
        )


def _ptr(page: int, para: int) -> ReadingPointer:
    return ReadingPointer(page_index=page, paragraph_index=para)


class TestSceneControllerCaching:
    @pytest.mark.asyncio
    async def test_returns_default_when_no_ai(self):
        controller = SceneController(ai=None)
        decision = await controller.evaluate(pointer=_ptr(1, 0), paragraph="test")
        assert decision == _DEFAULT_SCENE

    @pytest.mark.asyncio
    async def test_successful_call_caches_decision(self):
        ai = FakeAiBridge()
        controller = SceneController(ai=ai)
        
        decision1 = await controller.evaluate(pointer=_ptr(1, 0), paragraph="first")
        assert decision1.scene_mood == "suspense"
        assert len(ai.calls) == 1
        
        # Second call for the same paragraph hits cache, no AI call
        decision2 = await controller.evaluate(pointer=_ptr(1, 0), paragraph="first again")
        assert decision2 is decision1
        assert len(ai.calls) == 1
        
        # Call for a new paragraph makes a new AI call
        decision3 = await controller.evaluate(pointer=_ptr(1, 1), paragraph="second")
        assert decision3.scene_mood == "suspense"
        assert len(ai.calls) == 2

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self):
        ai = FakeAiBridge()
        controller = SceneController(ai=ai)
        
        await controller.evaluate(pointer=_ptr(1, 0), paragraph="first")
        assert len(ai.calls) == 1
        
        controller.invalidate()
        
        await controller.evaluate(pointer=_ptr(1, 0), paragraph="first")
        assert len(ai.calls) == 2


class TestSceneControllerDeduplication:
    @pytest.mark.asyncio
    async def test_concurrent_calls_for_same_key_cause_one_ai_call(self):
        ai = FakeAiBridge(delay_seconds=0.05)
        controller = SceneController(ai=ai)
        
        # Fire two evaluations for the same paragraph concurrently
        t1 = asyncio.create_task(controller.evaluate(pointer=_ptr(1, 0), paragraph="a"))
        t2 = asyncio.create_task(controller.evaluate(pointer=_ptr(1, 0), paragraph="b"))
        
        d1, d2 = await asyncio.gather(t1, t2)
        
        assert d1 is d2
        assert len(ai.calls) == 1
        assert ai.calls[0][0] == "a"  # The first one won the race

    @pytest.mark.asyncio
    async def test_concurrent_calls_for_different_keys_cause_two_ai_calls(self):
        ai = FakeAiBridge(delay_seconds=0.05)
        controller = SceneController(ai=ai)
        
        t1 = asyncio.create_task(controller.evaluate(pointer=_ptr(1, 0), paragraph="a"))
        t2 = asyncio.create_task(controller.evaluate(pointer=_ptr(1, 1), paragraph="b"))
        
        await asyncio.gather(t1, t2)
        
        assert len(ai.calls) == 2


class TestSceneControllerFallbacks:
    @pytest.mark.asyncio
    async def test_ai_failure_returns_default(self):
        ai = FakeAiBridge(outcomes=[FakeOutcome(ok=False, data={}, error="timeout")])
        controller = SceneController(ai=ai)
        
        decision = await controller.evaluate(pointer=_ptr(1, 0), paragraph="test")
        
        assert decision == _DEFAULT_SCENE
        assert len(ai.calls) == 1

    @pytest.mark.asyncio
    async def test_ai_failure_returns_last_valid_decision(self):
        ai = FakeAiBridge(outcomes=[
            FakeOutcome(ok=True, data={"scene_mood": "happy"}),
            FakeOutcome(ok=False, data={}, error="timeout")
        ])
        controller = SceneController(ai=ai)
        
        d1 = await controller.evaluate(pointer=_ptr(1, 0), paragraph="good")
        assert d1.scene_mood == "happy"
        
        d2 = await controller.evaluate(pointer=_ptr(1, 1), paragraph="bad")
        assert d2.scene_mood == "happy"  # The last valid decision
        
        assert len(ai.calls) == 2

    @pytest.mark.asyncio
    async def test_exception_during_ai_call_returns_fallback(self):
        class CrashingAi:
            async def scene_mood(self, **kwargs):
                raise ValueError("Boom")
                
        controller = SceneController(ai=CrashingAi())
        
        # Must not raise
        decision = await controller.evaluate(pointer=_ptr(1, 0), paragraph="test")
        assert decision == _DEFAULT_SCENE


class TestSceneControllerCleanup:
    @pytest.mark.asyncio
    async def test_cancelled_task_clears_in_flight_dict(self):
        ai = FakeAiBridge(delay_seconds=0.1)
        controller = SceneController(ai=ai)
        
        task = asyncio.create_task(controller.evaluate(pointer=_ptr(1, 0), paragraph="test"))
        
        # Give it a moment to start and register in _in_flight
        await asyncio.sleep(0.01)
        assert (1, 0) in controller._in_flight
        
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
            
        # The finally block must have cleaned it up
        assert (1, 0) not in controller._in_flight
        assert (1, 0) not in controller._cache


class TestPlaybackEngineSceneIntegration:
    @pytest.mark.asyncio
    async def test_playback_engine_evaluates_scene_with_ai_bridge(self):
        from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
        from backend.app.modules.audio_engine.speech_provider import FakeSpeechProvider, NullAudioSink
        from backend.app.modules.audio_engine.audio_profiles import NORMAL

        fast_profile = NORMAL

        ai = FakeAiBridge(outcomes=[
            FakeOutcome(
                ok=True,
                data={
                    "scene_mood": "mystery",
                    "emotion": "suspense",
                    "intensity": 0.7,
                    "audio_tag": "library",
                }
            )
        ])
        scene_controller = SceneController(ai=ai)

        class MockAmbientProvider:
            def __init__(self):
                self.decisions = []
                self.is_playing = True
                self.is_paused = False
                self.is_enabled = True
                self.current_tag = "neutral_narration"
                self.current_channel_id = 1

            async def crossfade(self, decision):
                self.decisions.append(decision)
                self.current_tag = decision.audio_tag

            async def pause(self):
                self.is_paused = True

            async def resume(self):
                self.is_paused = False

            async def stop(self):
                self.is_playing = False

        ambient = MockAmbientProvider()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            ambient_provider=ambient,
            scene_controller=scene_controller,
        )

        ptr = ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0)
        await engine.start(pointer=ptr, text="It was a dark and stormy night.", profile=fast_profile)
        await engine.wait_for_idle()

        # Let the background ambient evaluation settle
        await asyncio.sleep(0.05)

        assert len(ai.calls) == 1
        assert ai.calls[0][0] == "It was a dark and stormy night."
        assert len(ambient.decisions) == 1
        assert ambient.decisions[0].audio_tag == "library"

    @pytest.mark.asyncio
    async def test_ambient_ai_failure_does_not_block_or_fail_reading(self):
        from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
        from backend.app.modules.audio_engine.speech_provider import FakeSpeechProvider, NullAudioSink
        from backend.app.modules.audio_engine.audio_profiles import NORMAL

        fast_profile = NORMAL

        ai = FakeAiBridge(delay_seconds=0.05, outcomes=[
            FakeOutcome(ok=False, data={}, error="Groq API timeout")
        ])
        scene_controller = SceneController(ai=ai)

        class MockAmbientProvider:
            def __init__(self):
                self.decisions = []
                self.is_playing = True
                self.is_paused = False
                self.is_enabled = True
                self.current_tag = "neutral_narration"

            async def crossfade(self, decision):
                self.decisions.append(decision)

            async def pause(self):
                pass

            async def resume(self):
                pass

            async def stop(self):
                pass

        ambient = MockAmbientProvider()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            ambient_provider=ambient,
            scene_controller=scene_controller,
        )

        ptr = ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0)
        await engine.start(pointer=ptr, text="A simple peaceful sentence.", profile=fast_profile)
        await engine.wait_for_idle()

        assert engine.get_status().state is PlaybackState.FINISHED
        assert engine.get_status().statistics.sentences_spoken == 1
        assert engine.get_status().statistics.words_spoken == 4

