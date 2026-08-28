"""Record synchronized hardware frames, button transitions, and telemetry to a local dataset.

Usage:
    python scripts/record_live_dataset.py --output recordings/session_01 --max-frames 100
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.core.environment import load_environment
from backend.app.modules.image_receiver import Esp32Buttons, Esp32Camera

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def record_dataset(output_dir: Path, max_frames: int | None = None, interval: float = 0.1) -> None:
    """Record camera frames and button states into output directory."""
    load_environment()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    camera = Esp32Camera()
    buttons = Esp32Buttons()

    logger.info("Initializing recording to %s", output_dir)
    logger.info("Camera: %s, Buttons: %s", camera.capture_url, buttons.buttons_url)

    metadata_path = output_dir / "session_metadata.json"
    manifest_path = output_dir / "manifest.jsonl"

    session_meta = {
        "recorded_at": time.time(),
        "camera_url": camera.capture_url,
        "buttons_url": buttons.buttons_url,
        "target_interval_sec": interval,
    }
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(session_meta, f, indent=2)

    frame_count = 0
    start_time = time.time()

    try:
        with open(manifest_path, "a", encoding="utf-8") as manifest:
            while max_frames is None or frame_count < max_frames:
                loop_start = time.time()
                btn_state = buttons.read() if buttons.configured else None
                raw_frame = camera.frame() if camera.configured else None

                frame_entry = {
                    "frame_index": frame_count,
                    "timestamp": loop_start,
                    "elapsed_sec": loop_start - start_time,
                    "button_momentary": btn_state.momentary if btn_state else False,
                    "button_toggle": btn_state.toggle if btn_state else False,
                    "both_active": btn_state.both_active if btn_state else False,
                    "button_reachable": btn_state.reachable if btn_state else False,
                    "frame_file": None,
                }

                if raw_frame is not None:
                    frame_filename = f"frame_{frame_count:05d}.jpg"
                    frame_path = frames_dir / frame_filename
                    with open(frame_path, "wb") as img_file:
                        img_file.write(raw_frame)
                    frame_entry["frame_file"] = f"frames/{frame_filename}"

                manifest.write(json.dumps(frame_entry) + "\n")
                manifest.flush()

                frame_count += 1
                if frame_count % 20 == 0:
                    logger.info("Recorded %d frames...", frame_count)

                elapsed = time.time() - loop_start
                sleep_time = max(0.0, interval - elapsed)
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Recording stopped by operator.")

    logger.info("Recording complete. Saved %d frames to %s", frame_count, output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record live TaleTrace hardware data.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("recordings/test_capture"),
        help="Destination directory for recordings",
    )
    parser.add_argument(
        "--max-frames",
        "-n",
        type=int,
        default=None,
        help="Maximum frames to record (default: record until Ctrl-C)",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=float,
        default=0.1,
        help="Capture interval in seconds (default: 0.1s)",
    )
    args = parser.parse_args()

    record_dataset(args.output, max_frames=args.max_frames, interval=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
