"""TaleTrace Physical Audio & Reading Engine Demonstration.

Plays crystal clear live audio directly through the system speakers:
1. Edge TTS high-fidelity neural narration of the reconstructed reading text.
2. Background ambient environment music ('peaceful'/'tavern') mixed simultaneously.
3. Meaning Mode lookup demonstration with graceful pause, dictionary explanation, and resume.
"""

import os
import sys
import time
import asyncio
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pygame
from backend.app.core.environment import load_environment
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import EdgeSpeechProvider, SpeechRequest, LocalAudioSink
from backend.app.modules.audio_engine.ambient_cache import AmbientAssetCache
from backend.app.modules.audio_engine.local_ambient import LocalAmbientProvider
from backend.app.modules.audio_engine.scene_controller import SceneController
from backend.app.modules.audio_engine.models import ReadingPointer, PauseReason, PlaybackState, AmbientState

load_environment()

def init_audio():
    if not pygame.mixer.get_init():
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
    pygame.mixer.set_num_channels(8)

async def synthesize_to_file(provider: EdgeSpeechProvider, text: str, output_path: Path, voice: str = "en-US-AriaNeural"):
    req = SpeechRequest(text=text, voice=voice)
    resp = await provider.synthesize(req)
    with open(output_path, "wb") as f:
        f.write(resp.audio)
    return output_path

async def main():
    print("=" * 80)
    print("   TALETRACE PHYSICAL READING ENGINE & AUDIO DEMONSTRATION")
    print("=" * 80)
    print("Initializing high-fidelity stereo audio system...")
    init_audio()
    
    provider = EdgeSpeechProvider()
    ambient_cache = AmbientAssetCache(REPO_ROOT / "assets" / "audio")
    ambient_provider = LocalAmbientProvider(asset_cache=ambient_cache)
    scene_controller = SceneController()
    
    temp_dir = REPO_ROOT / "logs" / "audio_demo_cache"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    sentences = [
        "Let me tell you about a problem. It might make you feel better about what you do with your money, and less judgmental about what other people do with theirs.",
        "People do some crazy things with money. But no one is crazy.",
        "Everyone has their own unique experience with how the world works.",
        "And what you have experienced is more compelling than what you learn second-hand.",
    ]
    
    meaning_explanation = "Meaning Mode: Challenge. A demanding task or situation that tests someone's ability or character."
    
    print("\n[1/4] Pre-synthesizing high-fidelity neural voices (Edge-TTS)...")
    audio_files = []
    for idx, sent in enumerate(sentences, 1):
        p = temp_dir / f"sent_{idx}.mp3"
        print(f"  Synthesizing sentence {idx}...")
        await synthesize_to_file(provider, sent, p, voice="en-US-AriaNeural")
        audio_files.append(p)
        
    meaning_path = temp_dir / "meaning_mode.mp3"
    print("  Synthesizing Meaning Mode definition voice (GuyNeural)...")
    await synthesize_to_file(provider, meaning_explanation, meaning_path, voice="en-US-GuyNeural")
    
    print("\n" + "-" * 80)
    print("   NOW PLAYING DEMO THROUGH YOUR SPEAKERS / HEADPHONES")
    print("-" * 80)
    
    # 1. Start Ambient Background Music on Channel 1
    print("\n>> Step 1: Starting peaceful ambient background music...")
    ambient_path = REPO_ROOT / "assets" / "audio" / "peaceful.mp3"
    assert ambient_path.exists(), f"Ambient file {ambient_path} missing!"
    
    ambient_sound = pygame.mixer.Sound(str(ambient_path))
    ambient_channel = pygame.mixer.Channel(1)
    ambient_channel.set_volume(0.25)  # 25% background volume for reading comfort
    ambient_channel.play(ambient_sound, loops=-1)
    time.sleep(2.0)
    
    # 2. Reading Engine Narrates Sentence 1 & 2 on Channel 0
    tts_channel = pygame.mixer.Channel(0)
    tts_channel.set_volume(1.0)  # 100% volume for narration
    
    for idx in range(2):
        print(f"\n>> Reading Engine [Sentence {idx+1}]: \"{sentences[idx]}\"")
        snd = pygame.mixer.Sound(str(audio_files[idx]))
        tts_channel.play(snd)
        while tts_channel.get_busy():
            time.sleep(0.05)
        time.sleep(0.4)  # Natural sentence boundary pause
        
    # 3. Meaning Mode Interaction (Triggered by reader button press)
    print("\n" + "=" * 80)
    print(">> [INTERACTION EVENT] Reader pressed button on word: 'CHALLENGE'")
    print("   Pausing ambient music & reading narration for Meaning Mode...")
    print("=" * 80)
    
    # Duck ambient volume during Meaning Mode explanation
    ambient_channel.set_volume(0.08)
    time.sleep(0.3)
    
    print(f"\n>> Meaning Mode Audio: \"{meaning_explanation}\"")
    meaning_snd = pygame.mixer.Sound(str(meaning_path))
    tts_channel.play(meaning_snd)
    while tts_channel.get_busy():
        time.sleep(0.05)
    time.sleep(0.5)
    
    # 4. Resume Normal Reading
    print("\n>> Meaning Mode complete. Restoring ambient volume and resuming narration...")
    ambient_channel.set_volume(0.25)
    time.sleep(0.5)
    
    for idx in range(2, len(sentences)):
        print(f"\n>> Reading Engine [Sentence {idx+1}]: \"{sentences[idx]}\"")
        snd = pygame.mixer.Sound(str(audio_files[idx]))
        tts_channel.play(snd)
        while tts_channel.get_busy():
            time.sleep(0.05)
        time.sleep(0.4)
        
    print("\n>> Finishing session. Fading out ambient music...")
    ambient_channel.fadeout(1500)
    time.sleep(1.8)
    
    print("\n" + "=" * 80)
    print("   PHYSICAL AUDIO & READING ENGINE DEMO COMPLETE (ALL AUDIO HEARD)")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
