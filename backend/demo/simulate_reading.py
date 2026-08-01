"""Full reading-session rehearsal for the Reading Audio Engine.

Runs a complete session — start, reading updates, Meaning Mode, OCR refinement,
a stale frame, a paragraph advance, a page turn, session end — against the real
PlaybackEngine, with the unfinished modules replaced by fakes.

    python -m backend.demo.simulate_reading
    python -m backend.demo.simulate_reading --provider edge --realtime
    python -m backend.demo.simulate_reading --provider offline --realtime

Default is the fake provider with compressed time, so the whole session runs in
about a second and can be used as a smoke test. `--provider edge --realtime` is
the one to watch: real neural speech, real pauses, real interruptions.

The point is to hear whether the *behaviour* is right — whether Meaning Mode
truly cuts in mid-word, whether resume repeats the interrupted sentence, whether
an OCR refresh disturbs the sentence in flight. Unit tests cannot answer those.
"""

import argparse
import asyncio
import logging

from backend.app.modules.audio_engine.audio_profiles import get_profile
from backend.app.modules.audio_engine.models import PlaybackState
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import (
    FakeSpeechProvider,
    LocalAudioSink,
    NullAudioSink,
    get_provider,
)
from backend.demo import console
from backend.demo.fake_merge_memory import FakeMergeMemory
from backend.demo.fake_reading_engine import FakeReadingEngine
from backend.demo.simulate_events import run_timeline


def _build_engine(provider_name: str, profile_name: str):
    """Wire an engine for the chosen provider.

    Providers that speak directly to the device (offline/pyttsx3) return no
    bytes, so they get a null sink; byte-returning providers need a real one.
    """

    if provider_name == "fake":
        provider, sink = FakeSpeechProvider(), NullAudioSink()
    else:
        provider = get_provider(provider_name)
        sink = NullAudioSink() if provider_name == "offline" else LocalAudioSink()

    engine = PlaybackEngine(provider=provider, sink=sink, session_id="simulator")
    engine.set_profile(get_profile(profile_name))
    return engine


def _check(stats, final_state) -> list[tuple[bool, str, str]]:
    """Assert that each scripted event actually reached the engine.

    Every entry corresponds to one step in the timeline. Checking the summary
    rather than the console output is deliberate: the statistics are what the AI
    Engine will consume, so if a counter is wrong the rehearsal should fail even
    though the transcript looked convincing.

    The pause check is the one that matters most. A sink that discards audio
    returns instantly, the queue drains before Meaning Mode can interrupt
    anything, and the session still ends with a full-looking summary — the
    failure this run is meant to expose.
    """

    if stats is None:
        return [(False, "session summary", "no statistics were produced")]

    return [
        (
            stats.sentences_spoken > 0,
            "speech happened",
            f"{stats.sentences_spoken} sentences, {stats.words_spoken} words",
        ),
        (
            stats.reading_time_ms > 0,
            "reading clock ran",
            f"{stats.reading_time_ms / 1000:.1f}s",
        ),
        (
            stats.reading_updates >= 2,
            "pointer jumps applied",
            f"{stats.reading_updates} reading updates",
        ),
        (
            stats.meaning_mode_count >= 1,
            "Meaning Mode engaged",
            f"{stats.meaning_mode_count} lookup(s)",
        ),
        (
            stats.pause_count >= 1,
            "playback was interrupted mid-sentence",
            f"{stats.pause_count} pause(s)"
            if stats.pause_count
            else "nothing was interrupted — is audio actually playing?",
        ),
        (
            stats.queue_refreshes >= 1,
            "OCR refinement applied",
            f"{stats.queue_refreshes} queue refresh(es)",
        ),
        (
            stats.stale_updates_rejected >= 1,
            "stale OCR frame rejected",
            f"{stats.stale_updates_rejected} rejected",
        ),
        (
            stats.pages_read >= 2,
            "page turn carried the session",
            f"{stats.pages_read} pages — statistics survived the turn",
        ),
        (
            final_state is PlaybackState.IDLE,
            "session shut down cleanly",
            f"final state {final_state.value}",
        ),
    ]


async def main(args) -> int:
    memory = FakeMergeMemory.from_book()
    engine = _build_engine(args.provider, args.profile)
    reading = FakeReadingEngine(engine=engine, memory=memory)

    # Compressed time by default: the timeline's delays are in simulated
    # seconds, and a smoke test should not take ninety of them.
    scale = 1.0 if args.realtime else 0.02

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds * scale)

    def show_state() -> None:
        status = engine.get_status()
        console.status_line(status)
        if args.timeline:
            console.timeline(status, reading.current_text())

    console.banner("TALETRACE", "Reading Audio Engine — Session Rehearsal")
    console.field("Book", f"{memory.page_count} pages")
    console.field("Provider", engine.provider_name)
    console.field("Profile", args.profile)
    console.field("Mode", "real time" if args.realtime else f"compressed x{1 / scale:.0f}")
    console.field("Voice", args.voice or "provider default")

    console.event("Camera ON", "session starting at page 1, paragraph 0")
    await reading.start_session(profile=get_profile(args.profile), voice_id=args.voice)
    show_state()

    await run_timeline(reading, sleep=sleep, show_state=show_state)

    # Let the last page finish rather than cutting it off, but do not wait
    # forever if a provider stalls.
    await sleep(4)
    try:
        await engine.wait_for_idle(timeout=30 if args.realtime else 10)
    except asyncio.TimeoutError:
        print("\n    (still speaking; ending the session anyway)")

    console.event("Camera OFF", "stopping playback and generating the summary")
    stats = await reading.end_session()
    console.summary(stats)

    return console.verdict(_check(stats, engine.get_status().state))


def parse_args():
    parser = argparse.ArgumentParser(description="TaleTrace reading session simulator")
    parser.add_argument(
        "--provider",
        default="fake",
        choices=("fake", "edge", "offline"),
        help="fake = silent and fast; edge = real neural voice; offline = OS voices",
    )
    parser.add_argument(
        "--profile",
        default="normal",
        help="audio profile or reading mode (normal, adaptive, disability, study, novel, exam)",
    )
    parser.add_argument("--voice", default=None, help="provider voice id")
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="use the timeline's real delays instead of compressing them",
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="draw the paragraph with the current sentence marked",
    )
    parser.add_argument("--verbose", action="store_true", help="show engine log lines")
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    logging.basicConfig(
        level=logging.INFO if cli_args.verbose else logging.WARNING,
        format="%(message)s",
    )
    raise SystemExit(asyncio.run(main(cli_args)))
