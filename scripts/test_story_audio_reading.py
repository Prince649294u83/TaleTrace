"""TaleTrace Story Audio & Reading Engine Demonstration.

Plays a rich multi-scene story with real-time TTS narration and dynamic ambient music:
- Scene 1: Peaceful forest sanctuary (Ambient: 'peaceful')
- Scene 2: Bustling village tavern (Ambient crossfade: 'tavern')
- Scene 3: Meaning Mode word lookup on 'constellation' (Audio ducking + dictionary explanation + resume)
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
from backend.app.modules.audio_engine.models import (
    AudioProfile,
    SpeechRequest,
    EmphasisLevel,
    ReadingPointer,
)
from backend.app.modules.audio_engine.audio_profiles import NORMAL, NOVEL
from backend.app.modules.audio_engine.speech_provider import EdgeSpeechProvider

load_environment()

def init_audio():
    if not pygame.mixer.get_init():
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
    pygame.mixer.set_num_channels(8)

async def synthesize_sentence(provider: EdgeSpeechProvider, text: str, output_path: Path, voice: str = "en-US-AriaNeural", profile: AudioProfile = NOVEL):
    req = SpeechRequest(text=text, profile=profile, voice_id=voice)
    resp = await provider.synthesize(req)
    if not resp.ok or not resp.audio:
        raise RuntimeError(f"Failed to synthesize: {resp.error}")
    with open(output_path, "wb") as f:
        f.write(resp.audio)
    return output_path

async def run_story_audio_test():
    print("=" * 80)
    print("   TALETRACE IMMERSIVE STORY READING & AMBIENT AUDIO TEST")
    print("=" * 80)
    print("Initializing high-fidelity stereo audio channels...")
    init_audio()
    
    provider = EdgeSpeechProvider()
    cache_dir = REPO_ROOT / "logs" / "story_audio_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Story Definition: Multi-scene narrative
    story_sections = [
        {
            "scene": "Forest Sanctuary",
            "ambient_tag": "peaceful",
            "ambient_file": REPO_ROOT / "assets" / "audio" / "peaceful.mp3",
            "sentences": [
                "The evening mist settled quietly over the ancient whispering pines, bathing the forest in silvery moonlight.",
                "Deep within the clearing, an owl called into the dusk, and the ancient stone sanctuary began to glow with faint starlight."
            ]
        },
        {
            "scene": "The Wayfarer's Tavern",
            "ambient_tag": "tavern",
            "ambient_file": REPO_ROOT / "assets" / "audio" / "tavern.mp3",
            "sentences": [
                "Stepping through the heavy oak door, the quiet forest gave way to the warm amber glow of the mountain tavern.",
                "Laughter echoed over clinking tankards and the comforting crackle of a roaring stone hearth.",
                "The old traveler unfurled a weathered parchment map, pointing toward a shimmering constellation etched in golden ink."
            ]
        }
    ]
    
    meaning_word = "CONSTELLATION"
    meaning_text = "Meaning Mode: Constellation. A group of stars forming a recognizable pattern that is traditionally named after its apparent form or identified with a mythological figure."
    
    # 1. Pre-synthesis step
    print("\n[1/3] Synthesizing story narration with neural voices (en-US-AriaNeural)...")
    s_idx = 0
    all_sentences = []
    for section in story_sections:
        for text in section["sentences"]:
            s_idx += 1
            audio_path = cache_dir / f"story_sentence_{s_idx}.mp3"
            print(f"  Synthesizing Sentence {s_idx:02d} ({len(text.split())} words)...")
            await synthesize_sentence(provider, text, audio_path, voice="en-US-AriaNeural", profile=NOVEL)
            all_sentences.append({
                "index": s_idx,
                "text": text,
                "audio_path": audio_path,
                "scene": section["scene"],
                "ambient_file": section["ambient_file"],
                "ambient_tag": section["ambient_tag"]
            })
            
    meaning_audio_path = cache_dir / "meaning_mode_constellation.mp3"
    print(f"  Synthesizing Meaning Mode definition for '{meaning_word}' (en-US-GuyNeural)...")
    await synthesize_sentence(provider, meaning_text, meaning_audio_path, voice="en-US-GuyNeural", profile=NORMAL)
    
    print("\n" + "-" * 80)
    print("   [2/3] STARTING LIVE STORY PLAYBACK THROUGH YOUR SPEAKERS / HEADPHONES")
    print("-" * 80)
    
    # Pygame Channel allocation:
    # Channel 0: TTS Narration
    # Channel 1: Primary Ambient
    # Channel 2: Secondary Ambient (for smooth crossfades)
    tts_channel = pygame.mixer.Channel(0)
    ambient_channel = pygame.mixer.Channel(1)
    
    current_ambient_tag = None
    
    for item in all_sentences:
        # Check if scene changed -> Crossfade ambient music
        if item["ambient_tag"] != current_ambient_tag:
            print(f"\n>> [SCENE CHANGE] Entering '{item['scene']}' (Ambient: {item['ambient_tag']}.mp3)")
            if current_ambient_tag is not None:
                ambient_channel.fadeout(1200)
                time.sleep(0.4)
            
            amb_sound = pygame.mixer.Sound(str(item["ambient_file"]))
            ambient_channel.set_volume(0.28)  # 28% comfortable background volume
            ambient_channel.play(amb_sound, loops=-1, fade_ms=1000)
            current_ambient_tag = item["ambient_tag"]
            time.sleep(1.2)  # Let ambient establish mood before narration
        
        # Narration
        print(f"\n[Sentence {item['index']:02d}] Narrator: \"{item['text']}\"")
        snd = pygame.mixer.Sound(str(item["audio_path"]))
        tts_channel.set_volume(1.0)
        tts_channel.play(snd)
        
        while tts_channel.get_busy():
            time.sleep(0.05)
            
        time.sleep(0.35)  # Natural sentence breathing interval
        
        # Trigger Meaning Mode on sentence 5 (the constellation sentence)
        if item["index"] == 5:
            print("\n" + "=" * 80)
            print(f">> [BUTTON PRESS] Reader pointed at word: '{meaning_word}'")
            print("   Ducking ambient audio and pausing story for Meaning Mode...")
            print("=" * 80)
            
            # Duck ambient volume to 8%
            ambient_channel.set_volume(0.08)
            time.sleep(0.3)
            
            print(f"\n[Meaning Mode Definition] {meaning_text}")
            meaning_snd = pygame.mixer.Sound(str(meaning_audio_path))
            tts_channel.play(meaning_snd)
            while tts_channel.get_busy():
                time.sleep(0.05)
            time.sleep(0.6)
            
            # Restore ambient volume
            print("\n>> Meaning Mode concluded. Restoring tavern ambiance and concluding reading...")
            ambient_channel.set_volume(0.28)
            time.sleep(1.0)
            
    # Conclude session with smooth fadeout
    print("\n>> Story finished. Fading out ambient soundtrack...")
    ambient_channel.fadeout(2000)
    time.sleep(2.2)
    
    print("\n" + "=" * 80)
    print("   [3/3] STORY READING & AUDIO DEMONSTRATION COMPLETE (ALL AUDIO PLAYED)")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(run_story_audio_test())
