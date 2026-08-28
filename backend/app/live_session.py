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

Each device is detected on its own
----------------------------------
The camera and the buttons arrive, fail and get rewired independently, so they
are probed independently and substituted independently. A reachable camera with
unreachable buttons is the common half-built state, and it is the dangerous one:
the session runs, frames are read, and nothing can ever fire a reading update or
Meaning Mode — a blind session that looks like a working one. So the buttons fall
back to a scheduled rehearsal and the substitution is printed, never silent.

The camera has no fallback here on purpose. A live session with a virtual camera
is a simulation, `simulated_session` already is one, and quietly replaying JPEGs
under the banner "live session" is how a hardware bug survives a green test.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from backend.app.core.environment import load_environment
from backend.app.modules.database.recording import (
    READER_ID,
    hydrate_reading_speed,
    record_finished_session,
)
from backend.app.modules.image_receiver import Esp32Buttons, Esp32Camera
from backend.app.modules.image_receiver.protocols import ButtonSource, CameraSource
from backend.app.modules.image_receiver.virtual_buttons import ScriptedButtons
from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
from backend.app.modules.reading_engine.ai_bridge import bridge_for_session
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.modules.database.session import get_db
from backend.app.modules.database.models import Reader
from backend.app.modules.audio_engine.speech_provider import get_provider, LocalAudioSink
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.shared.groq_keys import (
    AI_ENGINE_VARIABLE,
    CHAT_MODEL_VARIABLE,
    MERGE_ENGINE_VARIABLE,
    ai_engine_key,
    chat_model,
    fast_model,
    merge_engine_key,
)

logger = logging.getLogger(__name__)

SESSION_ID = "live-session"

# How long the rehearsal waits between rounds of presses when the buttons are
# missing, and how long an open-ended session is assumed to run for scheduling
# purposes. Both are knobs rather than constants of the universe: a slower reader
# wants a longer cycle, and a rig on a bench wants a shorter one.
_REHEARSAL_CYCLE_SECONDS = 30.0
_REHEARSAL_HORIZON_SECONDS = 1800.0


import time

def _camera_live(camera: Esp32Camera) -> bool:
    """Whether a frame can actually be fetched, not merely whether a URL is set.

    A URL pointing at a device which is powered off is the most common failure and
    it looks exactly like a correct configuration until a frame is asked for.
    """
    if not camera.configured:
        return False
    
    for _ in range(3):
        if camera.frame() is not None:
            return True
        time.sleep(1.0)
    return False


def _buttons_live(buttons: Esp32Buttons) -> bool:
    """Whether the button endpoint answers. Same reasoning as `_camera_live`."""
    if not buttons.configured:
        return False
        
    for _ in range(3):
        if buttons.read().reachable:
            return True
        time.sleep(1.0)
    return False


def _groq_models_live(key: str) -> tuple[bool, str]:
    """Whether the configured model names still exist on Groq.

    Same reasoning as `_camera_live`, for a vendor instead of a device: a key that
    authenticates against a model that has been retired looks exactly like a
    working configuration, and the first sign of trouble is an `{"error": ...}`
    where the reader's explanation should be. Groq retired
    `llama-3.3-70b-versatile` and this row is what would have said so.

    One `models.list()` — no tokens, no completion, and it names the missing model
    rather than the variable, because the fix is usually to unset a stale
    `GROQ_MODEL` rather than to set one.
    """

    from groq import Groq

    wanted = {chat_model(), fast_model()}
    try:
        available = {model.id for model in Groq(api_key=key).models.list().data}
    except Exception as error:  # noqa: BLE001 — a preflight row, not a failure
        return False, f"could not list models ({type(error).__name__})"

    missing = sorted(wanted - available)
    if missing:
        return False, f"{', '.join(missing)} no longer on Groq — unset or update {CHAT_MODEL_VARIABLE}"
    return True, ", ".join(sorted(wanted))


def rehearsal_script(
    seconds: float | None = None,
    *,
    cycle_seconds: float = _REHEARSAL_CYCLE_SECONDS,
) -> tuple[tuple[float, str], ...]:
    """A repeating reader's rhythm, long enough to cover the whole run.

    One-shot scripts are wrong here. `simulated_session`'s fires four presses in
    the first twenty seconds because a simulated session *is* those twenty
    seconds; a live camera test runs for as long as someone is holding a book, and
    a schedule that goes quiet after 20s would leave the remaining ten minutes
    testing nothing but frame capture.

    Each cycle: point at a word, hold Meaning Mode long enough for the AI call to
    land and interrupt narration, release.
    """

    horizon = seconds if seconds and seconds > 0 else _REHEARSAL_HORIZON_SECONDS
    cycles = max(1, int(horizon / cycle_seconds) + 1)
    return tuple(
        entry
        for index in range(cycles)
        for entry in (
            (index * cycle_seconds + 10.0, "reading_update"),
            (index * cycle_seconds + 18.0, "meaning_on"),
            (index * cycle_seconds + 24.0, "meaning_off"),
        )
    )


def detect_devices(
    *,
    buttons_mode: str = "auto",
    seconds: float | None = None,
) -> tuple[CameraSource | None, ButtonSource | None, list[str]]:
    """Pick the real device where it answers and a stand-in where it does not.

    Returns `(camera, buttons, notes)`. A `None` camera means no rig answered and
    the caller must stop — see the module docstring for why there is no fallback.
    `notes` is what changed from the all-hardware default, for printing: a
    substitution the operator did not ask for and cannot see is how a live test
    ends up measuring the wrong thing.

    `buttons_mode` is the override. Detection is a guess about the physical world
    and a wrong guess during a hardware test is expensive, so `hardware` refuses
    to substitute and `virtual` refuses to detect.
    """

    notes: list[str] = []

    camera = Esp32Camera()
    if not _camera_live(camera):
        return None, None, notes

    buttons: ButtonSource
    if buttons_mode == "virtual":
        buttons = ScriptedButtons(rehearsal_script(seconds))
        notes.append("buttons: scheduled rehearsal (--buttons virtual)")
    elif buttons_mode == "hardware":
        # No probe: the operator has asserted the rig is there. A dead endpoint
        # then produces a session with no events, which is the correct outcome of
        # being told not to second-guess the wiring.
        buttons = Esp32Buttons()
    else:
        hardware = Esp32Buttons()
        if _buttons_live(hardware):
            buttons = hardware
        else:
            buttons = ScriptedButtons(rehearsal_script(seconds))
            notes.append(
                "buttons: not reachable — running a scheduled rehearsal instead, "
                "so the camera can still be tested end to end"
            )

    return camera, buttons, notes


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


def preflight(*, probe_models: bool = False) -> list[tuple[str, bool, str]]:
    """What the rig can and cannot do right now, as (name, ok, detail).

    Secrets are reported as present or absent and never printed. Each row names
    the consequence rather than the variable, because "GROQ_API_KEY missing" and
    "page text will be raw OCR" are the same fact and only one of them tells the
    operator whether to bother starting.

    `probe_models` asks Groq whether the configured model names still exist. Off by
    default and on for `--check`: the website polls this for its device tile and a
    vendor round-trip per poll buys nothing there, while an operator asking why
    nothing works needs the one answer no local check can give.
    """

    camera = Esp32Camera()
    buttons = Esp32Buttons()
    ocr_space = bool((os.environ.get("OCR_SPACE_API_KEY") or "").strip())
    vision = bool((os.environ.get("GOOGLE_VISION_API_KEY") or "").strip())
    ocr_ok = ocr_space or vision
    if ocr_space:
        ocr_detail = "OCR.Space Engine 2 (prototype)"
    elif vision:
        ocr_detail = "Google Vision (fallback)"
    else:
        ocr_detail = "no OCR key -- set OCR_SPACE_API_KEY or GOOGLE_VISION_API_KEY"
    merge_groq = GroqReconstructor().available
    ai_groq = bool(ai_engine_key())

    rows: list[tuple[str, bool, str]] = [
        (
            "OCR",
            ocr_ok,
            ocr_detail,
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

    # Only worth asking once a key exists, and only once: both engines' names come
    # from `groq_keys` and both keys see the same catalogue, so one call answers for
    # both. Skipped with no key, because the rows above already say why.
    key = ai_engine_key() or merge_engine_key()
    if probe_models and key:
        models_ok, models_detail = _groq_models_live(key)
        rows.append(("Groq — models", models_ok, models_detail))

    # Reached rather than merely configured: a URL that points at a device which
    # is powered off is the most common failure, and it looks exactly like a
    # correct configuration until a frame is asked for.
    if camera.configured:
        rows.append(("ESP32-CAM", _camera_live(camera), camera.capture_url))
    else:
        rows.append(("ESP32-CAM", False, "ESP32_CAM_CAPTURE_URL not set"))

    if buttons.configured:
        # Named for what its absence costs, not for the wire that is down: without
        # buttons a session still reads pages, it just cannot be asked anything.
        reachable = _buttons_live(buttons)
        rows.append(
            (
                "ESP32 buttons",
                reachable,
                buttons.buttons_url
                if reachable
                else f"{buttons.buttons_url} — no answer; --buttons virtual rehearses instead",
            )
        )
    else:
        rows.append(
            (
                "ESP32 buttons",
                False,
                "ESP32_BUTTONS_URL not set — --buttons virtual rehearses instead",
            )
        )

    return rows


def _report(rows: list[tuple[str, bool, str]]) -> None:
    width = max(len(name) for name, _, _ in rows)
    for name, ok, detail in rows:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name.ljust(width)}   {detail}")


async def _run(args: argparse.Namespace) -> int:
    rows = preflight(probe_models=args.check)

    print("TaleTrace — live session")
    _report(rows)
    print()

    if args.check:
        return 0 if all(ok for _, ok, _ in rows) else 1

    ready = dict((name, ok) for name, ok, _ in rows)
    if not ready["OCR"]:
        # The one hard stop. Everything else degrades to a worse session; this
        # degrades to a session with no text in it at all.
        print("Cannot start: OCR has no API key. Set OCR_SPACE_API_KEY (or GOOGLE_VISION_API_KEY) in .env")
        return 1

    camera, buttons, notes = detect_devices(
        buttons_mode=args.buttons, seconds=args.seconds
    )
    if camera is None or buttons is None:
        if args.wait_for_hardware:
            print("Waiting for camera to become available...")
            while True:
                await asyncio.sleep(2.0)
                camera, buttons, notes = detect_devices(
                    buttons_mode=args.buttons, seconds=args.seconds
                )
                if camera is not None and buttons is not None:
                    print("Camera detected. Starting session.")
                    break
        else:
            print("Cannot start: no camera. Check the device is powered and on the network.")
            return 1

    for note in notes:
        print(f"  {note}")
    if notes:
        print()

    # The baseline before the runtime, and the same service handed to it. `build`
    # makes its own `ReadingSpeedService` when it is not given one, so hydrating
    # and not passing it on would look exactly like not hydrating: the reader
    # measured at 220 wpm in the browser would be scored against the default 200
    # and every page would come back unrated.
    speed = hydrate_reading_speed()
    ai = bridge_for_session()
    
    # Read the chosen TTS voice from the reader's preferences
    db = next(get_db())
    reader = db.get(Reader, READER_ID)
    voice_id = reader.device_prefs.get("ttsVoice") if reader and reader.device_prefs else None
    provider = get_provider(voice_id=voice_id)
    audio = PlaybackEngine(session_id=SESSION_ID, provider=provider, sink=LocalAudioSink())

    if ai is None:
        print(f"  No {AI_ENGINE_VARIABLE}: Meaning Mode will pause but not explain,")
        print("  and the session will finish with no summary, flashcards or quiz.\n")

    runtime = ReadingRuntime.build_live(
        session_id=SESSION_ID,
        reader_id=READER_ID,
        audio=audio,
        ai=ai,
        speed=speed,
    )
    display = buttons if isinstance(buttons, Esp32Buttons) else None
    loop = DeviceLoop(runtime=runtime, camera=camera, buttons=buttons, display=display)

    print("Reading. Momentary button re-reads from where you point;")
    print("the toggle holds Meaning Mode. Ctrl-C to finish.\n")

    # Ticks rather than seconds, because the loop counts ticks. Roughly ten a
    # second, which is the reference's 0.1s delay.
    max_ticks = int(args.seconds * 10) if args.seconds else None
    analytics = await loop.run(max_ticks=max_ticks)

    # After the loop, not during it. A session is written once, whole — the
    # website should never show a reading that is still growing.
    row_id, note = record_finished_session(
        runtime.engine,
        analytics,
        name="Live Reading",
        source_reference=f"{camera.source_name} + {buttons.source_name}",
    )

    print()
    print(f"  devices         {camera.source_name} + {buttons.source_name}")
    print(f"  frames read     {loop.frames_processed}")
    # Failures matter more than the count. A session that read 4 frames out of 600
    # attempts is a Wi-Fi problem reported as a low page count, and the operator
    # spends the afternoon looking at the OCR.
    failures = getattr(camera, "failures", 0)
    if failures:
        print(f"  frames lost     {failures}  (camera unreachable or returned no image)")
    print(f"  gestures        {loop.gestures_run}")
    print(f"  pages           {analytics.pages_read}")
    print(f"  words           {analytics.words_read}")
    print(f"  session         {row_id or note}")
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
    parser.add_argument(
        "--wait-for-hardware",
        action="store_true",
        help="wait indefinitely for the camera to become available instead of exiting",
    )
    parser.add_argument(
        "--buttons",
        choices=("auto", "hardware", "virtual"),
        default="auto",
        help="auto falls back to a scheduled rehearsal when the buttons are not "
        "reachable; hardware never substitutes; virtual never probes (default: auto)",
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
