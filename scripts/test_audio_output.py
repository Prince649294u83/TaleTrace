"""Phase 4: Physical Audio Output Verification Script (A/B/C Test).

Runs 3 test phases:
- Test A: TTS narration only (no ambient).
- Test B: Ambient background audio only (no TTS).
- Test C: BOTH active simultaneously (concurrent audio test).

Also tests Pause, Resume, and Stop transitions while checking AudioRuntimeState.
"""

import sys
import asyncio
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.app.core.environment import load_environment
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import get_provider, LocalAudioSink
from backend.app.modules.audio_engine.ambient_cache import AmbientAssetCache
from backend.app.modules.audio_engine.local_ambient import LocalAmbientProvider
from backend.app.modules.audio_engine.scene_controller import SceneController
from backend.app.modules.audio_engine.models import (
    ReadingPointer,
    PlaybackState,
    AmbientState,
    PauseReason,
)

load_environment()

async def run_audio_output_test():
    print("=" * 70)
    print("   TALETRACE AUDIO OUTPUT VERIFICATION & CONCURRENCY GATE")
    print("=" * 70)
    
    ambient_cache = AmbientAssetCache(REPO_ROOT / "assets" / "audio")
    ambient_provider = LocalAmbientProvider(asset_cache=ambient_cache)
    scene_controller = SceneController()
    provider = get_provider()
    sink = LocalAudioSink()
    
    engine = PlaybackEngine(
        session_id="audio-test-run",
        provider=provider,
        sink=sink,
        ambient_provider=ambient_provider,
        scene_controller=scene_controller,
    )
    
    # 1. Check audio configuration
    config = engine.audio_configuration()
    print(f"\n[CONFIG] Engine Audio Configuration: {config}")
    assert config["provider"] is not None
    assert config["sink"] == "LocalAudioSink"
    assert config["ambient"] == "LocalAmbientProvider"
    assert config["scene"] == "SceneController"
    
    # -------------------------------------------------------------
    # TEST A: TTS Only (isolated narration)
    # -------------------------------------------------------------
    print("\n--- TEST A: TTS Narration Isolated ---")
    tts_engine = PlaybackEngine(
        session_id="tts-only",
        provider=provider,
        sink=sink,
    )
    ptr = ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0)
    test_text_a = "Testing TaleTrace primary text to speech narration channel."
    st = await tts_engine.start(pointer=ptr, text=test_text_a)
    print(f"  TTS started. State: {st.value}")
    assert st == PlaybackState.PLAYING
    await asyncio.sleep(2.0)
    await tts_engine.stop()
    print("  TEST A PASSED [TTS narration functional]")
    
    # -------------------------------------------------------------
    # TEST B: Ambient Only (isolated background loop)
    # -------------------------------------------------------------
    print("\n--- TEST B: Ambient Background Audio Isolated ---")
    decision = await scene_controller.evaluate(
        pointer=ptr,
        paragraph="The quiet forest was filled with the gentle sound of rainfall."
    )
    print(f"  Scene evaluated -> audio_tag={decision.audio_tag}, intensity={decision.intensity}")
    await ambient_provider.crossfade(decision)
    await asyncio.sleep(0.5)
    
    assert ambient_provider.is_playing, "Ambient provider must report is_playing=True"
    print(f"  Ambient playing on channel {ambient_provider.current_channel_id} (tag={ambient_provider.current_tag})")
    await asyncio.sleep(2.0)
    await ambient_provider.stop()
    print("  TEST B PASSED [Ambient loop functional]")
    
    # -------------------------------------------------------------
    # TEST C: BOTH TTS AND AMBIENT CONCURRENT
    # -------------------------------------------------------------
    print("\n--- TEST C: Concurrent TTS Narration + Ambient Background ---")
    test_text_c = "The Psychology of Money explores the subtle mental dynamics behind financial choices."
    st = await engine.start(pointer=ptr, text=test_text_c)
    assert st == PlaybackState.PLAYING
    
    # Allow fire-and-forget ambient evaluation to kick off
    await asyncio.sleep(0.5)
    
    runtime_st = engine.get_runtime_state()
    print(f"  Runtime State:")
    print(f"    TTS state: {runtime_st.tts_state.value}")
    print(f"    Ambient state: {runtime_st.ambient_state.value}")
    print(f"    Ambient asset: {runtime_st.ambient_asset}")
    print(f"    Ambient channel: {runtime_st.ambient_channel}")
    print(f"    Audio generation: {runtime_st.audio_generation}")
    
    assert runtime_st.tts_state == PlaybackState.PLAYING, "TTS must be playing"
    assert runtime_st.ambient_state == AmbientState.PLAYING, "Ambient must be concurrently playing"
    
    print("\n  >> Listening to simultaneous TTS and Ambient for 3 seconds...")
    await asyncio.sleep(3.0)
    
    # Test Pause (e.g. Meaning Mode triggered)
    print("\n  >> Testing Pause transition...")
    gen_before_pause = engine.audio_generation
    await engine.pause(reason=PauseReason.MEANING_MODE)
    st_paused = engine.get_runtime_state()
    print(f"    TTS after pause: {st_paused.tts_state.value}")
    print(f"    Ambient after pause: {st_paused.ambient_state.value}")
    print(f"    Gen token bumped: {gen_before_pause} -> {st_paused.audio_generation}")
    assert st_paused.tts_state == PlaybackState.PAUSED
    assert st_paused.ambient_state == AmbientState.PAUSED
    assert st_paused.audio_generation > gen_before_pause
    
    # Test Resume
    print("\n  >> Testing Resume transition...")
    gen_before_resume = engine.audio_generation
    await engine.resume()
    st_resumed = engine.get_runtime_state()
    print(f"    TTS after resume: {st_resumed.tts_state.value}")
    print(f"    Ambient after resume: {st_resumed.ambient_state.value}")
    assert st_resumed.tts_state == PlaybackState.PLAYING
    assert st_resumed.ambient_state == AmbientState.PLAYING
    assert st_resumed.audio_generation > gen_before_resume
    
    await asyncio.sleep(2.0)
    
    # Test Stop
    print("\n  >> Testing Stop transition...")
    await engine.stop()
    st_stopped = engine.get_runtime_state()
    print(f"    TTS after stop: {st_stopped.tts_state.value}")
    print(f"    Ambient after stop: {st_stopped.ambient_state.value}")
    assert st_stopped.tts_state == PlaybackState.IDLE
    assert st_stopped.ambient_state == AmbientState.STOPPED
    
    print("\n" + "=" * 70)
    print("   ALL AUDIO TESTS A/B/C & CONCURRENCY TRANSITIONS PASSED!")
    print("=" * 70)
    return True

if __name__ == "__main__":
    asyncio.run(run_audio_output_test())
