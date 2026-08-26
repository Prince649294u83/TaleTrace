"""Run a reading session with no ESP32 on the desk.

    python -m backend.app.simulated_session                        # generates its own pages
    python -m backend.app.simulated_session page.jpg              # one page, scripted reader
    python -m backend.app.simulated_session pages/ --minutes 15   # a folder, paced
    python -m backend.app.simulated_session page.jpg --realtime    # watch it happen
    python -m backend.app.simulated_session page.jpg --offline     # recorded OCR, no Vision call

With no arguments it generates four synthetic pages, uses their recorded Vision
responses and runs the whole chain — no OCR key, no dataset, no Vision call. That
is the one command someone new to the project can run to see the system work.

`--offline` is about OCR, and only OCR. The AI Engine follows its own key: with
`GROQ_API_KEY_1` set, an offline run still explains the word the reader points at
and still writes an end-of-session review, because that is two calls per session
rather than one per frame. With no keys at all, nothing leaves the machine.

The sibling of `live_session`, and deliberately a *thin* one. Both files build the
same runtime and hand it to the same `DeviceLoop`; they differ in three arguments:

    live_session        Esp32Camera      Esp32Buttons      RealClock
    simulated_session   VirtualCamera    ScriptedButtons   VirtualClock

Nothing else differs, and nothing downstream is told which it got. That is the
whole claim this file exists to make good on: if a simulated session and a live one
can diverge, it is because one of them was given a different runtime, and there is
exactly one place — right here — where that could happen.

What it is for
--------------
Three things the rig cannot give us on demand:

  * a fifteen-minute session in ten seconds, so pointer drift, queue growth and
    duplicated Merge Memory have somewhere to show up before a reader finds them;
  * the same page a hundred times over, or a hundred pages once, without a human
    holding a book;
  * a button press at exactly 4.0s into the session, every run, so a failure is
    reproducible instead of being a story about what someone's hand was doing.

What it is not for
------------------
It is not a substitute for `live_session --check`. A simulation proves the software
correct; it says nothing about whether the camera is on the network, whether the
Wi-Fi drops frames under load, or whether the toggle is wired to the pin the
firmware thinks it is. Those are hardware facts and only hardware answers them.

To drive the real camera with a scheduled reader instead of real buttons — half a
rig, which is how a rig usually arrives — use `live_session --buttons virtual`.
That is a live session, not this: a real clock, real frames, real Vision calls.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from backend.app.core.environment import load_environment
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import get_provider
from backend.app.modules.database.recording import (
    READER_ID,
    hydrate_reading_speed,
    record_finished_session,
)
from backend.app.modules.image_receiver import VirtualCamera
from backend.app.modules.image_receiver.virtual_buttons import ScriptedButtons
from backend.app.modules.reading_engine.ai_bridge import bridge_for_session
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared.clock import VirtualClock
from backend.app.shared.groq_keys import AI_ENGINE_VARIABLE

logger = logging.getLogger(__name__)

SESSION_ID = "simulated-session"

# Where `--target`-less runs put their generated pages. Under the cache directory
# because they are derived data: deleting them costs one regeneration.
_SYNTHETIC_DIR = Path(__file__).resolve().parents[2] / ".taletrace_cache" / "synthetic"
_SYNTHETIC_PAGES = 4

# How long a simulated reader holds one page before the camera offers the next.
# Twelve seconds is roughly a slow paragraph, and the point is only that it is
# *longer than a tick*: a page per tick would have the reader turning pages ten
# times a second, and every reading-speed number computed from that is nonsense.
_SECONDS_PER_PAGE = 12.0

# The scripted reader's rhythm, in session seconds from the first poll. Chosen to
# exercise the transitions rather than to look busy: a reading update once the
# first page has been merged, then a Meaning Mode hold long enough for the AI call
# to land and for narration to be interrupted mid-sentence, then release.
_SCRIPT: tuple[tuple[float, str], ...] = (
    (3.0, "reading_update"),
    (8.0, "meaning_on"),
    (14.0, "meaning_off"),
    (20.0, "reading_update"),
)


def _use_utf8() -> None:
    """Force UTF-8 on the console, for the same reason `live_session` does.

    A curly quote in a book's text is enough to raise `UnicodeEncodeError` from a
    print statement on a cp1252 console and take the loop down with it.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a tty
            pass


def collect_images(target: Path, *, limit: int | None = None) -> list[Path]:
    """The images to feed, from a file or a directory, in a stable order."""

    if target.is_dir():
        suffixes = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        found = sorted(
            (p for p in target.iterdir() if p.suffix.lower() in suffixes),
            key=lambda p: p.name,
        )
        if not found:
            raise FileNotFoundError(f"no images under {target}")
        return found[:limit] if limit else found

    if not target.is_file():
        raise FileNotFoundError(f"no such image or directory: {target}")
    return [target]


def build_session(
    images: list[Path],
    *,
    speed: float,
    offline: bool,
    seconds_per_page: float = _SECONDS_PER_PAGE,
    script: tuple[tuple[float, str], ...] = _SCRIPT,
    audio: Any = None,
    ai: Any = None,
) -> tuple[DeviceLoop, VirtualClock]:
    """Assemble Simulation Mode. The one place the virtual devices are chosen.

    Returns the loop and the clock, because a caller that wants to assert about
    session time needs the clock that measured it.

    `offline` swaps live Google Vision for the recorded responses under
    `.taletrace_cache/vision/` and drops the Groq half of the Merge Engine, so no
    per-frame call leaves the machine. Every other stage — the same preprocessing,
    the same parser, the same word indices — is untouched, because the cached bytes
    are keyed on exactly what the live provider would have been sent. That mode is
    for stress and stability runs, where a five-hundred-image sweep should not cost
    five hundred Vision calls to tell us the queue leaked. An image with no
    recorded response raises rather than reading as a blank page.

    It says nothing about `ai`, which the caller passes either way: the AI Engine
    is one explain and one review per *session*, not per frame, so there is nothing
    for a per-frame cost switch to save by turning it off.

    `audio` takes three values, and the difference between two of them matters:
    `None` builds a real `PlaybackEngine` (the default), an object is used as
    given, and `False` asks for silent reading with no narration at all.
    """

    clock = VirtualClock(speed=speed)

    # The reader's stored baseline, on the clock this session will measure with.
    # Hydrated here rather than in `_run` because the clock is made here, and a
    # service on the real clock inside a 60x session reports every wpm wrong by
    # the speed factor. Offline only: `build_live` takes no clock, so its engine
    # is on the real one and a virtual-clock service would disagree with it.
    reading_speed = hydrate_reading_speed(
        ReadingSpeedService(clock=clock.now) if offline else ReadingSpeedService()
    )

    camera = VirtualCamera.streaming(
        images,
        interval_seconds=seconds_per_page,
        clock=clock,
        # The last page is held rather than exhausted, so a session that outlasts
        # its images has the reader still looking at a book instead of at nothing.
        # An exhausted camera would leave the tail of the run measuring an idle
        # system and calling it stability.
        repeat_last=True,
    )
    buttons = ScriptedButtons(script, clock=clock)

    # A real PlaybackEngine, on the same virtual clock. `audio=None` reaches the
    # engine as "no narration configured", which silently skips all eight of its
    # audio call sites — the pause on Meaning Mode, the seek when a gesture moves
    # the pointer, the queue refresh when Merge Memory accepts better text — so a
    # session that proved the whole chain still proved nothing about narration.
    #
    # `fake` unless asked otherwise: it records what it was told to speak and
    # touches neither the network nor a speaker, which is what makes a replay
    # deterministic. `AUDIO_PROVIDER=edge` narrates out loud for real, which is
    # worth doing with `--realtime` and pointless at 60x.
    if audio is None:
        audio = PlaybackEngine(
            provider=get_provider(os.environ.get("AUDIO_PROVIDER") or "fake"),
            session_id=SESSION_ID,
            clock=clock.now,
        )
    elif audio is False:
        # Silent reading, asked for on purpose. The engine's audio call sites all
        # no-op on `None`, so that is what "no narration" looks like downstream —
        # but it cannot be the *default*, which is the distinction this branch
        # exists to keep: `None` means the caller did not choose and gets a real
        # engine. Those two being the same value is what left every simulated
        # session silent for a milestone.
        #
        # It is a real scenario, not just a test mode: a reader with TTS switched
        # off measures their own pace, so `words_read` comes from the pointer
        # instead of from what was spoken.
        audio = None

    if offline:
        from backend.app.modules.ocr.vision_cache import CachedVisionProvider

        runtime = ReadingRuntime.build(
            session_id=SESSION_ID,
            reader_id=READER_ID,
            ocr_provider=CachedVisionProvider(),
            audio=audio,
            ai=ai,
            speed=reading_speed,
            # `clock.now` rather than the clock object: `ReadingSpeedService` and
            # `ReadingEngine` take a plain `Callable[[], float]`, and passing the
            # virtual one is what keeps their elapsed times in the same world as
            # the loop's. Without it a session the loop believes ran for fifteen
            # minutes is one Reading Speed believes ran for six seconds, and every
            # WPM figure in the analytics is wrong by the speed factor.
            clock=clock.now,
        )
    else:
        runtime = ReadingRuntime.build_live(
            session_id=SESSION_ID,
            reader_id=READER_ID,
            audio=audio,
            ai=ai,
            speed=reading_speed,
        )

    loop = DeviceLoop(
        runtime=runtime,
        camera=camera,
        buttons=buttons,
        clock=clock,
        # The reference's one-second settle before a gesture capture. Kept, because
        # it is about the reader's hand and not about the code — and on a virtual
        # clock it costs `1.0 * speed` of real waiting, so keeping it is free.
        settle_seconds=1.0,
    )
    return loop, clock


def ensure_synthetic_pages(*, limit: int | None = None) -> list[Path]:
    """Generated pages, made on first use and reused afterwards.

    The zero-argument path. `scripts.synthetic_page` writes both the images and
    the Vision responses that describe them, so the offline chain has something to
    read without a key, a network or a photograph.

    Generated, not photographed: good for the pointer, the queue and Merge Memory,
    worthless for judging whether OCR can read a real page under a real lamp.
    """

    from scripts.synthetic_page import generate

    existing = sorted(_SYNTHETIC_DIR.glob("page_*.png")) if _SYNTHETIC_DIR.exists() else []
    if len(existing) < _SYNTHETIC_PAGES:
        print(f"  generating {_SYNTHETIC_PAGES} synthetic pages in {_SYNTHETIC_DIR}")
        generate(_SYNTHETIC_DIR, count=_SYNTHETIC_PAGES, force=False)
        print()
        existing = sorted(_SYNTHETIC_DIR.glob("page_*.png"))

    if not existing:
        raise FileNotFoundError(f"could not generate pages in {_SYNTHETIC_DIR}")
    return existing[:limit] if limit else existing


async def _run(args: argparse.Namespace) -> int:
    offline = args.offline
    if args.target:
        images = collect_images(Path(args.target).expanduser(), limit=args.limit)
    else:
        # No target: generate pages and use their recorded responses. Forced
        # offline, because the whole point is a run that needs no OCR key — a live
        # Vision call on a generated page would also be a waste of a real quota
        # to read text we already know the answer to.
        images = ensure_synthetic_pages(limit=args.limit)
        offline = True

    speed = 1.0 if args.realtime else args.speed
    seconds = args.minutes * 60.0

    # The AI Engine follows its own key, not `--offline`. Offline is a *per-frame*
    # cost switch — it exists so a five-hundred-image sweep does not cost five
    # hundred Vision calls — and the AI Engine is not a per-frame cost: it answers
    # one Meaning Mode press and writes one end-of-session review, so a scripted
    # run makes two calls whether OCR came from the network or from disk. Tying it
    # to `offline` would mean the only way to exercise the Meaning Mode chain was
    # to pay for OCR on every tick of the session.
    ai = bridge_for_session()

    print("TaleTrace — simulated session")
    print(f"  images      {len(images)}  ({images[0].name}"
          f"{f' … {images[-1].name}' if len(images) > 1 else ''})")
    print(f"  session     {seconds / 60:.0f} min of reading time")
    print(f"  clock       {'real time' if speed == 1.0 else f'{speed}x real per session second'}")
    print(f"  OCR         {'recorded Vision responses (offline)' if offline else 'Google Vision (live)'}")
    print(
        "  AI Engine   "
        + (
            "ready — Meaning Mode explains, the session ends with a summary"
            if ai is not None
            else f"off — {AI_ENGINE_VARIABLE} not set, so no explanations and no summary"
        )
    )
    provider = os.environ.get("AUDIO_PROVIDER") or "fake"
    print(
        f"  narration   {provider}"
        + (
            " — records what it would say, no sound and no network"
            if provider == "fake"
            else " — speaks out loud (AUDIO_PROVIDER)"
        )
    )
    print()

    loop, clock = build_session(images, speed=speed, offline=offline, ai=ai)

    # Ticks, not seconds, because the loop counts ticks and each advances the
    # virtual clock by `tick_seconds`. This is the same arithmetic `live_session`
    # does; there it lands on real seconds, here on session seconds.
    max_ticks = int(seconds / loop.tick_seconds)

    analytics = await loop.run(max_ticks=max_ticks)

    # Written to the same table as a live session, and named so the difference is
    # visible in the website's session list. Same table on purpose: a simulated
    # run is how the read path gets exercised without hardware, and a separate
    # table would mean the website's queries were never the ones under test.
    row_id, note = record_finished_session(
        loop.runtime.engine,
        analytics,
        name="Simulated Session",
        source_reference=f"{len(images)} image(s) from {images[0].parent}",
    )

    print()
    print(f"  session time    {clock.now():.1f}s over {clock.sleeps} ticks")
    print(f"  frames read     {loop.frames_processed}")
    print(f"  gestures        {loop.gestures_run}")
    print(f"  events          {len(loop.events_published)}")
    print(f"  pages           {analytics.pages_read}")
    print(f"  words           {analytics.words_read}")

    # Narration reported as sentences actually finished, not as "audio configured":
    # a queue that was built and never spoken, or one paused by Meaning Mode and
    # never resumed, both read as a working session everywhere else in this output.
    spoken = loop.runtime.engine.audio.final_statistics
    if spoken is not None:
        print(
            f"  narrated        {spoken.sentences_spoken} sentences, "
            f"{spoken.words_spoken} words"
        )
        print(
            f"  narration held  {spoken.meaning_mode_count} for Meaning Mode, "
            f"{spoken.pause_count} pauses, {spoken.queue_refreshes} queue refreshes"
        )

    print(f"  session         {row_id or note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a full reading session against virtual hardware."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="an image, or a directory of images to feed one page at a time. "
        "Omit it to generate synthetic pages and read them with no OCR key.",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=1.0,
        help="session length in reading time, not in waiting time (default: 1)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="real seconds waited per session second; 0 waits not at all (default: 0)",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="run at human speed, so the session can be watched. Same as --speed 1",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="replay adapter instead of Google Vision: no OCR key, no OCR network. "
        "Says nothing about the AI Engine, which follows its own key",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="use at most this many images from a directory",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show module logs")
    args = parser.parse_args(argv)

    _use_utf8()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Before anything resolves a credential, as in `live_session`: `build_live`
    # reads the keys at construction, so loading afterwards would produce a
    # keyless session on a machine with a perfectly good .env.
    load_environment()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"  {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
