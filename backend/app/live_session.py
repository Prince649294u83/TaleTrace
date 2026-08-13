"""Run a live reading session on the ESP32 rig.

    python -m backend.app.live_session                  # a full session
    python -m backend.app.live_session --check          # is the rig reachable?
    python -m backend.app.live_session --seconds 120    # stop after two minutes

The replacement for `python main_controller.py`, and the only production entry
point: `ReadingRuntime.build_live` assembles the chain, `DeviceLoop` drives it
from the hardware, and everything in between is the same code the test suite
runs. Nothing here reimplements a module — if this file grows a decision about
reading, that decision is in the wrong place.

    ESP32-CAM ─frames─┐
                      ├─► DeviceLoop ─► ReadingRuntime ─► the modules
    ESP32 buttons ────┘

What this adds over the demo: the demo replays recorded word boxes so it can run
on a laptop. This calls Google Vision on real frames from a real camera, and is
the only way to find out whether the rig, the key and the network agree.

--check exists because "the session produced no text" has four causes that look
identical from the outside — no camera, no key, no buttons, no book in frame —
and finding out which one by starting a session and waiting is the slow way.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from backend.app.core.environment import load_environment
from backend.app.modules.image_receiver import Esp32Buttons, Esp32Camera
from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.shared.groq_keys import (
    AI_ENGINE_VARIABLE,
    MERGE_ENGINE_VARIABLE,
    ai_engine_key,
)

logger = logging.getLogger(__name__)

SESSION_ID = "live-session"
READER_ID = "live-reader"


def _use_utf8() -> None:
    """Force UTF-8 on the console, as the reference's `main_controller` did.

    Windows consoles default to cp1252, which cannot encode the punctuation in
    the output below — or, more to the point, the text of a book that has a
    curly quote in it. Without this a `UnicodeEncodeError` from a print statement
    takes down the reading loop, which is a spectacular way to lose a session.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a tty
            pass


def preflight() -> list[tuple[str, bool, str]]:
    """What the rig can and cannot do right now, as (name, ok, detail).

    Secrets are reported as present or absent and never printed. Each row names
    the consequence rather than the variable, because "GROQ_API_KEY missing" and
    "page text will be raw OCR" are the same fact and only one of them tells the
    operator whether to bother starting.
    """

    camera = Esp32Camera()
    buttons = Esp32Buttons()
    vision = bool((os.environ.get("GOOGLE_VISION_API_KEY") or "").strip())
    merge_groq = GroqReconstructor().available
    ai_groq = bool(ai_engine_key())

    rows: list[tuple[str, bool, str]] = [
        (
            "Google Vision",
            vision,
            "ready" if vision else "GOOGLE_VISION_API_KEY not set — no text can be read",
        ),
        # Two rows, because the two keys fail independently and the operator
        # needs to know which half is down: one means rough page text, the other
        # means Meaning Mode has nothing to say.
        (
            "Groq — Merge Engine",
            merge_groq,
            "ready"
            if merge_groq
            else f"{MERGE_ENGINE_VARIABLE} not set — raw OCR text, geometry-only page turns",
        ),
        (
            "Groq — AI Engine",
            ai_groq,
            "ready"
            if ai_groq
            else f"{AI_ENGINE_VARIABLE} not set — Meaning Mode cannot explain a word",
        ),
    ]

    # Reached rather than merely configured: a URL that points at a device which
    # is powered off is the most common failure, and it looks exactly like a
    # correct configuration until a frame is asked for.
    if camera.configured:
        rows.append(
            ("ESP32-CAM", camera.frame() is not None, camera.capture_url)
        )
    else:
        rows.append(("ESP32-CAM", False, "ESP32_CAM_CAPTURE_URL not set"))

    if buttons.configured:
        rows.append(("ESP32 buttons", buttons.read().reachable, buttons.buttons_url))
    else:
        rows.append(("ESP32 buttons", False, "ESP32_BUTTONS_URL not set"))

    return rows


def _report(rows: list[tuple[str, bool, str]]) -> None:
    width = max(len(name) for name, _, _ in rows)
    for name, ok, detail in rows:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name.ljust(width)}   {detail}")


async def _run(args: argparse.Namespace) -> int:
    rows = preflight()

    print("TaleTrace — live session")
    _report(rows)
    print()

    if args.check:
        return 0 if all(ok for _, ok, _ in rows) else 1

    ready = dict((name, ok) for name, ok, _ in rows)
    if not ready["Google Vision"]:
        # The one hard stop. Everything else degrades to a worse session; this
        # degrades to a session with no text in it at all.
        print("Cannot start: OCR has no API key. Set GOOGLE_VISION_API_KEY in .env")
        return 1
    if not ready["ESP32-CAM"]:
        print("Cannot start: no camera. Check the device is powered and on the network.")
        return 1

    runtime = ReadingRuntime.build_live(session_id=SESSION_ID, reader_id=READER_ID)
    loop = DeviceLoop(runtime=runtime)

    print("Reading. Momentary button re-reads from where you point;")
    print("the toggle holds Meaning Mode. Ctrl-C to finish.\n")

    # Ticks rather than seconds, because the loop counts ticks. Roughly ten a
    # second, which is the reference's 0.1s delay.
    max_ticks = int(args.seconds * 10) if args.seconds else None
    analytics = await loop.run(max_ticks=max_ticks)

    print()
    print(f"  frames read     {loop.frames_processed}")
    print(f"  gestures        {loop.gestures_run}")
    print(f"  pages           {analytics.pages_read}")
    print(f"  words           {analytics.words_read}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a live TaleTrace reading session.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what the rig can do and exit, without starting a session",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after this many seconds instead of running until Ctrl-C",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show module logs")
    args = parser.parse_args(argv)

    _use_utf8()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Before anything reads a key. `build_live` resolves credentials at
    # construction, so loading after that point would silently produce a
    # keyless session on a machine that has a perfectly good .env.
    load_environment()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        # `DeviceLoop.run` handles the interrupt that arrives mid-loop and still
        # finishes the session. This catches one that arrives before it starts.
        return 130


if __name__ == "__main__":
    sys.exit(main())
