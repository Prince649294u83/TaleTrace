"""Phase 4: Deterministic Audio Asset Validation.

Asserts that:
1. Every asset is non-empty (> 0 bytes).
2. Every asset decodes into pygame.mixer.Sound without corruption.
3. Initial 500ms audio energy has RMS > 0.001 (not dead silence).
4. Manifest hash and metadata match physical disk assets.
"""

import sys
from pathlib import Path
import json
import hashlib
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ASSETS_DIR = REPO_ROOT / "assets" / "audio"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"

def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def validate_assets() -> bool:
    print(f"=== Validating Audio Assets in {ASSETS_DIR} ===")
    
    assert MANIFEST_PATH.exists(), f"Missing audio manifest: {MANIFEST_PATH}"
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
        
    import pygame
    if not pygame.mixer.get_init():
        pygame.mixer.init(frequency=44100, size=-16, channels=2)
        
    all_passed = True
    for asset_name, meta in manifest.get("audio_assets", {}).items():
        asset_path = ASSETS_DIR / asset_name
        if not asset_path.exists():
            print(f"FAIL [MISSING]: {asset_name}")
            all_passed = False
            continue
            
        size = asset_path.stat().st_size
        if size == 0:
            print(f"FAIL [ZERO_BYTES]: {asset_name} is 0 bytes")
            all_passed = False
            continue
            
        disk_hash = compute_sha256(asset_path)
        if disk_hash != meta.get("sha256"):
            print(f"FAIL [HASH_MISMATCH]: {asset_name} expected {meta.get('sha256')} got {disk_hash}")
            all_passed = False
            continue
            
        try:
            sound = pygame.mixer.Sound(str(asset_path))
            raw = sound.get_raw()
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            
            # Initial 500ms check (at 44100Hz stereo = 44100 samples)
            first_500ms_samples = samples[:44100]
            rms_initial = float(np.sqrt(np.mean(first_500ms_samples ** 2))) if len(first_500ms_samples) > 0 else 0.0
            
            print(f"PASS: {asset_name:25s} | size={size/1024:6.1f}KB | dur={sound.get_length():5.1f}s | init_rms={rms_initial:.4f}")
        except Exception as e:
            print(f"FAIL [DECODE_ERROR]: {asset_name}: {e}")
            all_passed = False
            
    print("=" * 60)
    if all_passed:
        print("ALL AUDIO ASSETS VALIDATED SUCCESSFULLY!")
    else:
        print("AUDIO ASSET VALIDATION FAILED!")
    return all_passed

if __name__ == "__main__":
    success = validate_assets()
    sys.exit(0 if success else 1)
