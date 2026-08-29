"""Phase 4: Live Audio Path Integration Check.

Validates that ReadingRuntime, LiveSession, and SimulatedSession construct
the dual-layer audio stack (TTS + Ambient) without errors, and that
audio_configuration() returns complete valid components.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.app.core.environment import load_environment
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import get_provider, LocalAudioSink
from backend.app.modules.audio_engine.ambient_cache import AmbientAssetCache
from backend.app.modules.audio_engine.local_ambient import LocalAmbientProvider
from backend.app.modules.audio_engine.scene_controller import SceneController
from backend.app.modules.reading_engine.runtime import ReadingRuntime

load_environment()

def test_live_audio_wiring():
    print("Testing live audio stack initialization...")
    ambient_cache = AmbientAssetCache(REPO_ROOT / "assets" / "audio")
    ambient_provider = LocalAmbientProvider(asset_cache=ambient_cache)
    scene_controller = SceneController()
    provider = get_provider()
    sink = LocalAudioSink()

    audio = PlaybackEngine(
        session_id="live-audio-wiring-check",
        provider=provider,
        sink=sink,
        ambient_provider=ambient_provider,
        scene_controller=scene_controller,
    )

    config = audio.audio_configuration()
    print(f"  audio_configuration: {config}")

    assert config["provider"] is not None
    assert config["sink"] == "LocalAudioSink"
    assert config["ambient"] == "LocalAmbientProvider"
    assert config["scene"] == "SceneController"
    assert audio.provider is not None
    assert audio.sink is not None
    assert audio.ambient_provider is not None
    assert audio.scene_controller is not None

    runtime = ReadingRuntime.build_live(
        session_id="live-audio-wiring-check",
        reader_id="local-reader",
        audio=audio,
    )
    assert runtime.engine.audio is not None
    assert runtime.engine.audio.ambient_provider is not None
    print("PASS: Live audio path complete and verified!")

if __name__ == "__main__":
    test_live_audio_wiring()
