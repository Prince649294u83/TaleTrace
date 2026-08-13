"""A long reading session, sampled tick by tick, looking for the slow failures.

    python -m scripts.stress_session page.jpg --minutes 60
    python -m scripts.stress_session pages/ --minutes 240 --json stress.json
    python -m scripts.stress_session page.jpg --minutes 15 --verbose

What this asks that the other harnesses cannot
----------------------------------------------
`validate_pipeline` proves one frame survives the chain and `validate_corpus`
proves many frames each survive it once. Both are *short*. Neither can see the
class of defect that only appears after the four-hundredth tick:

  - Merge Memory accumulating an already-accumulated page, so the word count
    climbs while the reader stares at one page;
  - the reading pointer walking backwards, or off the end of the text;
  - the audio queue refilled faster than it drains, growing with tick count
    rather than with the page;
  - the runtime's pending-event list never draining;
  - the heap growing per tick with nothing to show for it.

Every one of those is invisible in a ten-second run and fatal in an hour-long
one, which is the length a real reader actually reads for.

Why it needs no photographs
---------------------------
The camera holds the last image once the sequence is exhausted, so one cached page
sustains an arbitrarily long session — and that is the interesting load, not a
weakness: a reader looks at one page for minutes at a time, and *the same page
arriving again and again* is exactly what makes Merge Memory duplicate. OCR runs
offline against recorded Vision responses, so a four-hour sweep costs nothing and
tells us about the software rather than about the network.

Time is virtual. `--minutes 240` is four hours of session time in a few seconds of
waiting, and every clock in the runtime is the same virtual one, so the analytics
describe a four-hour session rather than a four-second one.

What it is not
--------------
Not an accuracy harness. It never asks whether the right word was selected or
whether OCR read the page correctly — `validate_corpus` owns that question, and
mixing the two would mean a blurred photograph failed the stability run.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import sys
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.environment import load_environment  # noqa: E402
from backend.app.modules.audio_engine.models import AudioProfile  # noqa: E402
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine  # noqa: E402
from backend.app.modules.audio_engine.speech_provider import (  # noqa: E402
    FakeSpeechProvider,
    NullAudioSink,
)
from backend.app.simulated_session import build_session, collect_images  # noqa: E402

# No real synthesis and no real pauses. The queue still fills, drains and is
# rebuilt on every page turn exactly as it would with Edge on the other end —
# which is the half this harness measures. Waiting for speech would make a
# four-hour session take four hours.
SILENT_PROFILE = AudioProfile(name="stress", rate=1.0, pause_after_sentence_ms=0)


@dataclass
class Sample:
    """The whole of the observable session state at one tick. Nothing derived."""

    tick: int
    session_seconds: float

    memory_words: int = 0
    memory_version: int = 0
    memory_pages: int = 0

    page_index: int = 0
    paragraph_index: int = 0
    sentence_index: int = 0

    runtime_events: int = 0
    pending_events: int = 0
    audio_queued: int = 0
    audio_state: str = ""
    # Spoken and refreshed are what tell a queue that never filled apart from one
    # that filled and drained. Depth alone reads as zero in both cases, and
    # reporting "narration never started" for a session that spoke every sentence
    # it was given would be a false statement about a working engine.
    audio_spoken: int = 0
    audio_refreshes: int = 0
    audio_stale_rejected: int = 0

    heap_kb: float = 0.0
    gc_objects: int = 0

    def row(self) -> str:
        return (
            f"  {self.tick:>6} {self.session_seconds:>9.1f}s "
            f"{self.memory_words:>7} {self.memory_version:>5} {self.memory_pages:>4} "
            f"{self.page_index:>4}.{self.paragraph_index:<3} "
            f"{self.runtime_events:>6} {self.pending_events:>5} "
            f"{self.audio_queued:>5} {self.audio_spoken:>6} {self.audio_state[:9]:<9} "
            f"{self.heap_kb:>9.0f} {self.gc_objects:>8}"
        )


@dataclass
class Check:
    label: str
    passed: bool
    detail: str = ""


@dataclass
class StressReport:
    samples: list[Sample] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, label: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(label, bool(passed), detail))

    def note(self, message: str) -> None:
        self.notes.append(message)


def observe(loop: Any, clock: Any, tick: int) -> Sample:
    """One sample. Reads state; changes none of it.

    Deliberately reads through the same public surfaces a dashboard would, rather
    than reaching into private fields: a leak that only shows up in `_tallies` and
    not in anything a caller can see is not one this harness should claim to have
    found.
    """

    runtime = loop.runtime
    engine = runtime.engine
    memory = engine.memory
    pointer = engine.state.pointer

    sample = Sample(
        tick=tick,
        session_seconds=clock.now(),
        memory_words=memory.total_words,
        memory_version=memory.version,
        memory_pages=memory.page_count,
        page_index=pointer.page_index,
        paragraph_index=pointer.paragraph_index,
        sentence_index=pointer.sentence_index,
        runtime_events=len(engine.events),
        pending_events=len(runtime.pending),
        gc_objects=len(gc.get_objects()),
    )

    if engine.audio is not None:
        status = engine.audio.get_status()
        sample.audio_queued = status.queued_sentences
        sample.audio_state = str(getattr(status.state, "value", status.state))
        stats = status.statistics
        if stats is not None:
            sample.audio_spoken = stats.sentences_spoken
            sample.audio_refreshes = stats.queue_refreshes
            sample.audio_stale_rejected = stats.stale_updates_rejected

    current, _peak = tracemalloc.get_traced_memory()
    sample.heap_kb = current / 1024.0
    return sample


async def run_session(args: argparse.Namespace, report: StressReport) -> Any:
    """Drive the real `DeviceLoop` tick by tick, sampling as it goes.

    Ticks are driven here rather than through `loop.run()` for one reason: `run`
    is a closed loop with nowhere to observe from, and a stability harness that
    can only see the beginning and the end cannot tell a leak from a large
    constant.

    Everything else is `simulated_session.build_session` — the same assembly a
    simulated session uses, imported rather than rebuilt, so a divergence between
    this harness and Simulation Mode is impossible by construction.
    """

    images = collect_images(Path(args.target).expanduser(), limit=args.limit)

    audio: Any = None
    if not args.no_audio:
        audio = PlaybackEngine(
            provider=FakeSpeechProvider(), sink=NullAudioSink(), auto_advance=True
        )
        audio.set_profile(SILENT_PROFILE)

    loop, clock = build_session(
        images,
        speed=args.speed,
        offline=not args.live,
        seconds_per_page=args.seconds_per_page,
        audio=audio,
    )

    max_ticks = int((args.minutes * 60.0) / loop.tick_seconds)
    every = max(1, max_ticks // args.samples)

    print("TaleTrace — stress and stability run")
    print(f"  images            {len(images)}  (held after the last, so the session outlasts them)")
    print(f"  session           {args.minutes:.0f} min  ({max_ticks} ticks of {loop.tick_seconds}s)")
    print(f"  OCR               {'Google Vision (live)' if args.live else 'recorded responses (offline)'}")
    print(f"  audio             {'none' if audio is None else 'real queue, silent provider'}")
    print(f"  sampling          every {every} ticks")
    print()

    tracemalloc.start()
    await loop.runtime.engine.start_session()
    report.samples.append(observe(loop, clock, 0))

    try:
        for tick in range(1, max_ticks + 1):
            await loop.tick()
            await clock.sleep(loop.tick_seconds)
            if tick % every == 0 or tick == max_ticks:
                report.samples.append(observe(loop, clock, tick))
                if args.verbose:
                    print(report.samples[-1].row())
    finally:
        analytics = await loop.finish()
        report.samples.append(observe(loop, clock, max_ticks))
        tracemalloc.stop()

    return loop, clock, analytics


# ------------------------------------------------------------------- invariants


def assess(report: StressReport, loop: Any, analytics: Any, args: argparse.Namespace) -> None:
    """Turn the samples into verdicts. Every check is a property, not a threshold guess."""

    samples = report.samples
    if len(samples) < 4:
        report.check("enough samples to judge stability", False, f"only {len(samples)}")
        return

    half = len(samples) // 2
    tail = samples[half:]

    # 1. The duplication bug, stated exactly. The camera holds one page for the
    #    whole back half of the run, so Merge Memory has nothing new to be told.
    #    A word count that keeps climbing there is a page being appended to
    #    itself — the failure `apply_frame`'s `whole_page` flag exists to prevent.
    words_first, words_last = tail[0].memory_words, tail[-1].memory_words
    growth = words_last - words_first
    report.check(
        "Merge Memory stops growing once the page stops changing",
        growth <= args.word_drift,
        f"{words_first} -> {words_last} words across the back half of the session "
        f"(+{growth}, allowed {args.word_drift})",
    )

    # 2. Version is a monotonic counter that other modules compare against. A
    #    decrease means a stale frame overwrote a newer page.
    versions = [s.memory_version for s in samples]
    report.check(
        "the Merge Memory version never goes backwards",
        all(b >= a for a, b in zip(versions, versions[1:])),
        "a stale frame overwrote newer text",
    )

    # 3. Pointer drift. Pages are physical sheets: a reader turns them forwards.
    #    A gesture can move the pointer *within* a page, which is why only the
    #    page index is asserted monotonic and the paragraph index is not.
    pages = [s.page_index for s in samples]
    report.check(
        "the reading pointer never turns back to an earlier page",
        all(b >= a for a, b in zip(pages, pages[1:])),
        f"pages seen: {sorted(set(pages))}",
    )

    # 4. A pending list that only grows is a backlog nobody is draining, and the
    #    events in it are the ones that tell Audio and AI what the reader did.
    pendings = [s.pending_events for s in samples]
    report.check(
        "the runtime's pending events drain rather than pile up",
        max(pendings) <= args.max_pending,
        f"peaked at {max(pendings)} (allowed {args.max_pending})",
    )

    # 5. The audio queue is rebuilt per page, so its depth is a property of the
    #    page. Depth that tracks tick count instead is a refill outpacing a drain.
    #
    #    Depth is read together with sentences spoken, because a queue sitting at
    #    zero means two opposite things: nothing was ever queued, or everything
    #    queued was spoken. Only the second is the engine working, and calling
    #    both "never filled" would report a healthy session as an untested one.
    queued = [s.audio_queued for s in samples]
    spoken = samples[-1].audio_spoken
    refreshes = samples[-1].audio_refreshes
    if spoken or refreshes:
        report.check(
            "the audio queue is bounded by the page, not by how long the session ran",
            max(queued) <= args.max_queue,
            f"peaked at {max(queued)} sentences (allowed {args.max_queue})",
        )
        # A refresh rejected as stale is correct behaviour on an out-of-order
        # frame and a defect if it is most of them, so the count is reported
        # rather than judged — the harness cannot tell which without the frames.
        report.note(
            f"audio spoke {spoken} sentence(s) across {refreshes} queue refresh(es), "
            f"{samples[-1].audio_stale_rejected} rejected as stale; the queue "
            f"drained to {queued[-1]} rather than accumulating"
        )
    else:
        report.note(
            "no audio activity at all — neither queued nor spoken, so queue growth "
            "is untested rather than proven bounded"
        )

    # 6. The heap. Compared as a *rate* between the two halves rather than as an
    #    absolute: a session legitimately allocates as it reads a page, and the
    #    question is whether it keeps doing so after the page stops changing.
    first_half = samples[:half]
    early_rate = _rate_per_tick(first_half, lambda s: s.heap_kb)
    late_rate = _rate_per_tick(tail, lambda s: s.heap_kb)
    report.check(
        "the heap stops growing once the session reaches steady state",
        late_rate <= max(args.heap_kb_per_tick, early_rate),
        f"{late_rate:.3f} KB/tick in the back half vs {early_rate:.3f} in the front "
        f"(allowed {args.heap_kb_per_tick})",
    )

    # 7. The runtime event log has no eviction and is returned by `engine.events`
    #    as a copy. Growth is expected; unbounded growth over a long session is a
    #    cost worth knowing, so it is reported as a rate rather than judged.
    events_rate = _rate_per_tick(samples, lambda s: float(s.runtime_events))
    report.note(
        f"the runtime event log grows {events_rate:.3f} entries/tick and is never "
        f"evicted — {samples[-1].runtime_events} entries after "
        f"{samples[-1].session_seconds / 60:.0f} minutes"
    )

    # 8. The session has to have actually happened. Without this every check
    #    above passes trivially on a session that read nothing at all.
    report.check(
        "the session actually read something",
        loop.frames_processed > 0 and samples[-1].memory_words > 0,
        f"{loop.frames_processed} frames processed, "
        f"{samples[-1].memory_words} words held",
    )

    # Re-reading an unchanged page still bumps Merge Memory's version, and every
    # bump wakes Reading Speed, Focus Analytics and the audio refresh for text
    # that did not change. Reported rather than failed: it is stable — the word
    # count above proves that — so it is a cost, not a correctness bug, and
    # whether the cost is worth a dirty check is a product decision.
    versions_per_frame = (
        samples[-1].memory_version / loop.frames_processed if loop.frames_processed else 0.0
    )
    report.note(
        f"{samples[-1].memory_version} Merge Memory versions for {loop.frames_processed} "
        f"frames ({versions_per_frame:.2f} per frame) across "
        f"{len(set(s.page_index for s in samples))} distinct page(s) — an unchanged "
        f"page still bumps the version and notifies every consumer"
    )


def _rate_per_tick(samples: list[Sample], value: Any) -> float:
    """Change in `value` per tick across a span of samples."""

    if len(samples) < 2:
        return 0.0
    ticks = samples[-1].tick - samples[0].tick
    if ticks <= 0:
        return 0.0
    return (value(samples[-1]) - value(samples[0])) / ticks


# ----------------------------------------------------------------------- output


def print_samples(report: StressReport) -> None:
    print(f"\n{'=' * 110}")
    print("  SAMPLES")
    print("=" * 110)
    print(
        f"  {'tick':>6} {'session':>10} {'words':>7} {'ver':>5} {'pg':>4} "
        f"{'pointer':>8} {'events':>6} {'pend':>5} {'queue':>5} {'spoke':>6} {'audio':<9} "
        f"{'heap KB':>9} {'objects':>8}"
    )
    for sample in report.samples:
        print(sample.row())


def print_summary(report: StressReport, analytics: Any, loop: Any) -> bool:
    print(f"\n{'=' * 110}")
    print("  SESSION")
    print("=" * 110)
    print(f"  frames processed  {loop.frames_processed}")
    print(f"  frames skipped    {loop.frames_skipped}  (button polls without an OCR pass)")
    print(f"  gestures          {loop.gestures_run}")
    print(f"  pages read        {analytics.pages_read}")
    print(f"  words read        {analytics.words_read}")

    print(f"\n{'=' * 110}")
    print("  ACCEPTANCE")
    print("=" * 110)
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        # Detail only on failure: a passing check that prints its failure message
        # reads as though it failed.
        line = f"  [{mark}] {check.label}"
        if not check.passed and check.detail:
            line += f" — {check.detail}"
        print(line)

    for note in report.notes:
        print(f"  note: {note}")

    failed = [c for c in report.checks if not c.passed]
    print()
    print(f"  {len(report.checks) - len(failed)} of {len(report.checks)} checks passed")
    return not failed


async def _main(args: argparse.Namespace) -> int:
    report = StressReport()
    loop, _clock, analytics = await run_session(args, report)

    assess(report, loop, analytics, args)

    if not args.verbose:
        print_samples(report)
    ok = print_summary(report, analytics, loop)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "minutes": args.minutes,
                    "passed": ok,
                    "frames_processed": loop.frames_processed,
                    "gestures_run": loop.gestures_run,
                    "samples": [vars(s) for s in report.samples],
                    "checks": [vars(c) for c in report.checks],
                    "notes": report.notes,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n  written to {out}")

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="an image, or a directory of images")
    parser.add_argument(
        "--minutes", type=float, default=60.0, help="session length in session time (default: 60)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="real seconds waited per session second; 0 waits not at all (default: 0)",
    )
    parser.add_argument(
        "--seconds-per-page",
        type=float,
        default=12.0,
        help="how long the simulated reader holds one page (default: 12)",
    )
    parser.add_argument("--limit", type=int, default=None, help="use at most this many images")
    parser.add_argument(
        "--samples", type=int, default=40, help="how many samples to take across the run"
    )
    parser.add_argument(
        "--live", action="store_true", help="call Google Vision instead of replaying cached responses"
    )
    parser.add_argument(
        "--no-audio", action="store_true", help="run without a playback engine attached"
    )
    parser.add_argument(
        "--word-drift",
        type=int,
        default=0,
        help="words Merge Memory may gain in the back half of the session (default: 0)",
    )
    parser.add_argument(
        "--max-pending", type=int, default=64, help="peak allowed runtime pending events"
    )
    parser.add_argument(
        "--max-queue", type=int, default=200, help="peak allowed queued sentences"
    )
    parser.add_argument(
        "--heap-kb-per-tick",
        type=float,
        default=1.0,
        help="heap growth per tick allowed in the back half (default: 1.0)",
    )
    parser.add_argument("--verbose", action="store_true", help="print each sample as it is taken")
    parser.add_argument("--json", default="", help="also write the full result to this file")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    load_environment()
    try:
        return asyncio.run(_main(args))
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"  {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
