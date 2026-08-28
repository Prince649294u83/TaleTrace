"""Replay a recorded TaleTrace dataset against the gesture and OCR pipeline.

Usage:
    python scripts/replay_dataset.py --input recordings/session_01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.modules.gesture_engine import GesturePipeline
from backend.app.modules.gesture_engine.detector import detect_finger
from backend.app.modules.gesture_engine.selection_models import SelectionConfig
from backend.app.modules.image_receiver import Esp32Camera

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def replay_dataset(input_dir: Path, realtime: bool = False) -> None:
    """Replay recorded manifest and evaluate gesture detector against saved frames."""
    manifest_path = input_dir / "manifest.jsonl"
    if not manifest_path.exists():
        logger.error("Manifest not found at %s", manifest_path)
        sys.exit(1)

    camera = Esp32Camera()
    config = SelectionConfig()
    pipeline = GesturePipeline(config=config)

    detected_count = 0
    total_frames = 0

    with open(manifest_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    logger.info("Starting replay of %d frames from %s", len(lines), input_dir)
    prev_timestamp = None

    for line in lines:
        entry = json.loads(line)
        frame_idx = entry.get("frame_index", total_frames)
        frame_file = entry.get("frame_file")
        ts = entry.get("timestamp", 0.0)

        if realtime and prev_timestamp is not None:
            delay = max(0.0, ts - prev_timestamp)
            time.sleep(delay)
        prev_timestamp = ts

        if frame_file:
            img_path = input_dir / frame_file
            if img_path.exists():
                with open(img_path, "rb") as img_f:
                    raw_bytes = img_f.read()
                image = camera.decode(raw_bytes)
                if image is not None:
                    point = detect_finger(image, config)
                    if point is not None:
                        detected_count += 1
                        logger.info(
                            "Frame %d: finger at (%.1f, %.1f) conf=%.2f [%s]",
                            frame_idx,
                            point.x,
                            point.y,
                            point.confidence,
                            point.detection_method,
                        )
                    else:
                        logger.debug("Frame %d: no finger detected", frame_idx)

        total_frames += 1

    logger.info(
        "Replay complete: %d/%d frames had detected fingers (%.1f%%)",
        detected_count,
        total_frames,
        (detected_count / max(total_frames, 1)) * 100.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay recorded TaleTrace dataset.")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Directory containing session recordings and manifest.jsonl",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Replay at original recording speed",
    )
    args = parser.parse_args()

    replay_dataset(args.input, realtime=args.realtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
