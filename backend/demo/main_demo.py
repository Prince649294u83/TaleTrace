"""TaleTrace Simulator — one entry point for the whole pipeline.

    python -m backend.demo.main_demo                 # auto, compressed time
    python -m backend.demo.main_demo --realtime      # auto, human speed
    python -m backend.demo.main_demo -i              # drive it yourself
    python -m backend.demo.main_demo -i --tts        # with narration

The point of this file is to show what the finished product does before the
finished product exists. No OCR, no ESP32, no camera — those are fakes. Everything
downstream of them is the real implementation, running the real code paths.

Pipeline, identical in every mode:

      book.txt ──► FakeMergeMemory ──► SimulatedReadingEngine
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                    Reading Speed        Audio Engine         Dashboard
                    (predictions)         (playback)        (live console)

Only the leftmost box is fake. When OCR lands, `FakeMergeMemory` is replaced and
nothing else moves; when Gesture lands, the calls to `gesture_to()` come from a
camera instead of the keyboard.

Two modes:

  Auto      A scripted reader crosses three pages at known paces — page 1 on
            pace, page 2 slow *with* friction, page 3 slow with *none*. The last
            two are the interesting pair: identical timings, opposite verdicts.
            Ends in a pass/fail checklist, so this doubles as a smoke test.

  Interactive (-i)   You are the reader. Type `help` for the command list.

Compressed time is the default because the honest version of this run takes about
six minutes of sitting still. The compressed clock is injected into
`ReadingSpeedService`, so every number it reports is measured against simulated
time — the same arithmetic on a faster clock, not a fudged approximation.
"""

import argparse
import asyncio
import logging
import sys
import time

from backend.app.modules.audio_engine.audio_profiles import get_profile
from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.audio_engine.speech_provider import (
    FakeSpeechProvider,
    NullAudioSink,
)
from backend.app.modules.reading_speed.models import DifficultyLevel
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.demo import console
from backend.demo.dashboard import Dashboard
from backend.demo.fake_merge_memory import FakeMergeMemory
from backend.demo.fake_session_engine import SimulatedReadingEngine

READER_ID = "demo-reader"
BOOK_TITLE = "The Lighthouse"


def _sentence_at(reading) -> str:
    """The text of the sentence the pointer names, for the dashboard.

    Segmented here rather than stored, using the same segmenter the Audio Engine
    speaks from — a cached copy could show a sentence Merge Memory has since
    refined, which is exactly the disagreement the screen exists to expose.
    """

    pointer = reading.pointer
    for chunk in segment_sentences(reading.current_text()):
        if chunk.pointer.sentence_index == pointer.sentence_index:
            return chunk.text
    return ""

# A measured baseline rather than the default, because difficulty analysis
# refuses to rate anything against a guess — and rating pages is the whole point
# of the auto run. 180 wpm over a 60s passage.
CALIBRATION_WORDS = 180
CALIBRATION_MS = 60_000

# How the scripted reader crosses each page, as a fraction of their baseline.
# Pages 2 and 3 are both slow by the same margin; only page 2 has friction to
# explain it. That pair is what the checklist at the end actually tests.
#
# Any page not named here is read on pace, so the script does not have to be
# rewritten when the book gains a page.
PAGE_PACES = {1: 1.00, 2: 0.45, 3: 0.55}


class SimulatedClock:
    """A clock that only moves when the simulation says so.

    Injected into `ReadingSpeedService` so compressed mode measures simulated
    time rather than wall time. Without this the fast run would report a reader
    who covered three pages in under a second, and every difficulty verdict would
    be nonsense.

    `sleep()` is the only thing that advances it, which makes the whole run
    deterministic: the same script produces the same numbers every time, so the
    checklist at the end is a real assertion rather than a coin flip.
    """

    def __init__(self, *, speedup: float = 1.0, real: bool = False) -> None:
        self._now = 0.0
        self._speedup = speedup
        self._real = real

    def __call__(self) -> float:
        """The current time in seconds. Matches `time.monotonic`'s contract."""

        return time.monotonic() if self._real else self._now

    def sleep(self, seconds: float) -> None:
        """Advance the clock, sleeping for real only in realtime mode."""

        if self._real:
            time.sleep(seconds / self._speedup)
        else:
            self._now += seconds


# ═══════════════════════════════════════════════════════════════ auto mode


class AutoSimulation:
    """A scripted reader crossing the book, driven entirely through events.

    Every state change goes through `SimulatedReadingEngine`. This class never
    touches Reading Speed or the Audio Engine directly, which is the constraint
    worth keeping: it means what you watch on screen is produced by the same
    call sequence the real Gesture Engine will make.
    """

    def __init__(
        self,
        reading: SimulatedReadingEngine,
        dashboard: Dashboard,
        clock: SimulatedClock,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._reading = reading
        self._dashboard = dashboard
        self._clock = clock
        self._loop = loop
        self._fired: set[int] = set()

    def run(self) -> None:
        self._plan_script()

        self._dashboard.event("CALIBRATED", f"180 wpm from a {CALIBRATION_WORDS}-word passage")
        self._dashboard.event("SESSION_STARTED", "page 1")

        content = self._reading.content

        for index in range(content.sentence_count):
            span = content.sentences[index]

            # Time the sentence at the page's scripted pace. Duration comes from
            # the same content map Reading Speed measures against, so a deviation
            # on screen is a real disagreement rather than two fakes drifting.
            wpm = 180.0 * PAGE_PACES.get(span.pointer.page_index, 1.0)
            self._read_for((span.word_count / wpm) * 60.0)

            self._await(self._reading.advance_sentence())
            self._scripted_event(index)

        # A last beat so the final page has time on the clock before it closes.
        self._read_for(2.0)
        self._dashboard.event("SESSION_FINISHED", "reader closed the book")

    def _read_for(self, seconds: float) -> None:
        """Spend `seconds` reading, repainting once a second while it passes."""

        remaining = seconds
        while remaining > 0:
            step = min(1.0, remaining)
            self._clock.sleep(step)
            remaining -= step
            self._paint()

    def _paint(self) -> None:
        sid = self._reading.session_id
        speed = self._reading.speed
        self._dashboard.draw(
            state=self._reading.state,
            snapshot=speed.tracker(sid).snapshot(),
            prediction=speed.predict(sid),
            baseline=speed.baseline_for(READER_ID),
            observed_wpm=speed.observed_wpm(sid),
            audio=None,
            difficulties=speed.page_difficulties(sid),
            book_title=BOOK_TITLE,
            chapter=self._reading.memory.chapter(self._reading.pointer.page_index),
            page_count=self._reading.memory.page_count,
            sentence=_sentence_at(self._reading),
            memory_version=self._reading.memory.version,
            session_seconds=self._clock(),
        )

    def _await(self, coro):
        return self._loop.run_until_complete(coro)

    def _due(self, index: int) -> bool:
        """Whether a scripted event should fire, once and only once.

        A gesture correction moves the pointer backwards, so the same sentence
        index comes round again — without this guard the correction re-fires and
        the reader never leaves the page.
        """

        if index in self._fired:
            return False
        self._fired.add(index)
        return True

    def _plan_script(self) -> None:
        """Work out which sentence indices the scripted events fire on.

        Derived from the content map rather than hardcoded, because a literal
        index silently means a different page the moment `book.txt` changes. It
        did: an earlier version fired the page-2 gesture correction on index 15,
        which had drifted onto page 3 — so the correction jumped the reader
        *backwards* onto a page they had finished, and the tracker duly closed
        page 3 and reopened page 2. That produced a fourth page observation and a
        duplicate verdict, from a script that still looked right.

        The rule each event actually depends on is a position within a page, so
        that is what gets computed here.
        """

        content = self._reading.content
        by_page: dict[int, list[int]] = {}
        for index, span in enumerate(content.sentences):
            by_page.setdefault(span.pointer.page_index, []).append(index)

        page2 = by_page.get(2, [])
        page3 = by_page.get(3, [])

        # Meaning Mode early on page 2, so the lookup lands on the page it is
        # meant to be evidence about.
        self._meaning_at = page2[1] if len(page2) > 1 else None

        # The correction targets an earlier sentence on the *same* page: a
        # correction is the reader fixing a wrong pointer, not turning back a page.
        #
        # Scripted events fire *after* `advance_sentence()`, so the index chosen
        # here must be one whose successor is still on page 2 — page2[-1] would
        # already have turned the page, and the correction would then drag the
        # reader off page 3 and back onto a page they had finished.
        self._correct_at = page2[-2] if len(page2) > 1 else None
        self._correct_to = (
            content.sentences[page2[len(page2) // 2]].pointer if page2 else None
        )

        # The unexplained pause belongs in the middle of page 3.
        self._pause_at = page3[len(page3) // 2] if page3 else None

    def _scripted_event(self, index: int) -> None:
        """The friction that makes pages 2 and 3 tell different stories."""

        # Page 2: the reader hits a word they do not know.
        if index == self._meaning_at and self._due(index):
            self._dashboard.event("MEANING_MODE_ON", "reader tapped 'lantern'")
            self._await(self._reading.meaning_mode_on())
            self._read_for(6.0)
            self._dashboard.event("LOOKUP_COMPLETED", "definition delivered")
            self._reading.lookup_completed("lantern")
            self._await(self._reading.meaning_mode_off())

        # Page 2: the pointer was wrong and the reader drags it back.
        if index == self._correct_at and self._due(index) and self._correct_to:
            target = self._correct_to
            self._dashboard.event("GESTURE", f"corrected back to s{target.sentence_index}")
            self._await(self._reading.gesture_to(target, corrected=True))

        # Page 3: twenty seconds away from the book, with nothing to show for it.
        # Same slow timing as page 2, no friction — the case analytics must refuse
        # to call difficult.
        if index == self._pause_at and self._due(index):
            self._dashboard.event("SESSION_PAUSED", "book set down (20s)")
            self._await(self._reading.pause())
            self._read_for(20.0)
            self._dashboard.event("SESSION_RESUMED", "picked back up")
            self._await(self._reading.resume())


def _run_auto(args: argparse.Namespace) -> int:
    memory = FakeMergeMemory.from_book()
    clock = SimulatedClock(real=args.realtime)

    console.banner("TALETRACE SIMULATOR", "full pipeline, no hardware")
    console.field("Book", f"{BOOK_TITLE} - {memory.page_count} pages")
    console.field("Clock", "real time" if args.realtime else "compressed")
    console.field("Script", "p1 on pace | p2 slow+friction | p3 slow, no friction")
    print()

    # The clock is injected here, which is what makes compressed mode honest:
    # the tracker's two clocks read from it, so reported reading time is
    # simulated time rather than the handful of milliseconds this actually takes.
    speed = ReadingSpeedService(clock=clock)
    speed.calibrate(READER_ID, word_count=CALIBRATION_WORDS, elapsed_ms=CALIBRATION_MS)

    reading = SimulatedReadingEngine(
        session_id="sim-auto",
        reader_id=READER_ID,
        memory=memory,
        speed=speed,
        tts=None,
    )

    loop = asyncio.new_event_loop()
    dashboard = Dashboard()
    try:
        loop.run_until_complete(reading.start_session(profile=get_profile("normal")))
        AutoSimulation(reading, dashboard, clock, loop=loop).run()
        # Through the Reading Engine, not `speed.finish_session()`: ending a
        # session is a session event, and routing around the owner here would
        # leave the state saying the session is still open.
        summary = loop.run_until_complete(reading.finish_session())
    finally:
        dashboard.stop()
        loop.close()

    _print_summary(summary)
    return console.verdict(_checks(summary))


def _checks(summary) -> list[tuple[bool, str, str]]:
    """What this run has to demonstrate to count as working.

    The last two are the pair that matters. Pages 2 and 3 are read at almost the
    same slow pace; the only difference is that page 2 has lookups and a pointer
    correction behind it. If both come back the same, difficulty inference is
    reading the clock and nothing else.
    """

    pages = {page.page_index: page for page in summary.pages}
    p2, p3 = pages.get(2), pages.get(3)

    # Every page is expected exactly once. A page appearing twice means the
    # pointer went backwards across a page boundary and the tracker closed and
    # reopened it — which is how the scripted gesture correction was caught
    # firing on the wrong page.
    counted = [page.page_index for page in summary.pages]
    duplicates = sorted({p for p in counted if counted.count(p) > 1})

    return [
        (summary.words_read > 0, "words were counted", f"{summary.words_read} words"),
        (
            not duplicates,
            "each page recorded once",
            f"{len(summary.pages)} observations"
            + (f", duplicated {duplicates}" if duplicates else ""),
        ),
        (summary.lookup_count >= 1, "lookup recorded", f"{summary.lookup_count}"),
        (summary.session_wpm > 0, "session pace computed", f"{summary.session_wpm:.0f} wpm"),
        (
            summary.reading_duration_ms < summary.wall_duration_ms,
            "reading clock excluded the pause",
            f"{summary.reading_duration_ms / 1000:.0f}s reading "
            f"< {summary.wall_duration_ms / 1000:.0f}s wall",
        ),
        (
            p2 is not None and p2.difficulty in (DifficultyLevel.MEDIUM, DifficultyLevel.HIGH),
            "slow page WITH friction -> difficult",
            f"page 2 = {p2.difficulty.value}" if p2 else "page 2 missing",
        ),
        (
            p3 is not None and p3.difficulty is DifficultyLevel.UNKNOWN,
            "slow page WITHOUT friction -> UNKNOWN",
            f"page 3 = {p3.difficulty.value}" if p3 else "page 3 missing",
        ),
    ]


# ═════════════════════════════════════════════════════════════ interactive

HELP = [
    ("play / resume", "Resume reading"),
    ("pause", "Set the book down (reading clock stops)"),
    ("next / n", "Read the next sentence"),
    ("meaning", "Toggle Meaning Mode (clock stops, counts as friction)"),
    ("lookup [word]", "Record a completed lookup"),
    ("gesture N", "Move the pointer to sentence N on this page"),
    ("gesture N back", "Move it back — counts as a correction"),
    ("page", "Turn to the next page"),
    ("ocr", "Deliver a better OCR frame for this page"),
    ("stale", "Deliver an out-of-order frame (should be refused)"),
    ("tts on / tts off", "Turn narration on or off"),
    ("camera off / on", "Stop or start the ESP32 stream (blinds gesture + OCR)"),
    ("summary", "End the session and show the analytics"),
    ("help / quit", "This list / exit"),
]


async def _interactive(reading: SimulatedReadingEngine, dashboard: Dashboard) -> int:
    loop = asyncio.get_running_loop()
    dashboard.event("SESSION_STARTED", "type 'help' for commands")

    async def repaint_every_second() -> None:
        """The live view. Reads state; never changes any.

        Everything shown is fetched fresh each pass rather than cached, so the
        screen cannot display something the system no longer believes.
        """

        while True:
            await asyncio.sleep(1.0)
            sid = reading.session_id
            speed = reading.speed
            audio = reading.audio_status()
            dashboard.draw(
                state=reading.state,
                snapshot=speed.tracker(sid).snapshot(),
                prediction=speed.predict(sid),
                baseline=speed.baseline_for(READER_ID),
                observed_wpm=speed.observed_wpm(sid),
                audio=audio,
                difficulties=speed.page_difficulties(sid),
                book_title=BOOK_TITLE,
                chapter=reading.memory.chapter(reading.pointer.page_index),
                page_count=reading.memory.page_count,
                sentence=_sentence_at(reading),
                memory_version=reading.memory.version,
                interactive=True,
            )

    ticker = asyncio.create_task(repaint_every_second())

    try:
        while True:
            try:
                line = await loop.run_in_executor(None, input, "tale> ")
            except (EOFError, KeyboardInterrupt):
                break

            command = line.strip().lower()
            if not command:
                continue
            if command in ("q", "quit", "exit"):
                break

            try:
                await _dispatch(command, reading, dashboard)
            except Exception as error:  # noqa: BLE001 - a demo must not die on a typo
                dashboard.event("ERROR", str(error))
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

    # `summary` is non-None only if the session was already finished from the
    # keyboard, in which case the summary is on screen and reprinting it here
    # would show the same numbers twice.
    already_shown = reading.summary is not None
    summary = await reading.finish_session()
    dashboard.stop()
    if not already_shown:
        _print_summary(summary)
    return 0


async def _dispatch(
    command: str, reading: SimulatedReadingEngine, dashboard: Dashboard
) -> None:
    """Turn one typed line into one Reading Engine event.

    Every branch calls a method on the Reading Engine and nothing else. That is
    deliberate: it keeps the keyboard on exactly the footing the Gesture Engine
    will have, so nothing here is a shortcut that the real system cannot take.
    """

    parts = command.split()
    verb = parts[0]

    if verb in ("help", "h", "?"):
        dashboard.stop()
        print()
        for name, description in HELP:
            print(f"  {name:<18} {description}")
        print()

    elif verb in ("play", "resume", "r"):
        await reading.resume()
        dashboard.event("SESSION_RESUMED", "")

    elif verb == "pause":
        await reading.pause()
        dashboard.event("SESSION_PAUSED", "reading clock stopped")

    elif verb in ("next", "n"):
        moved = await reading.advance_sentence()
        dashboard.event("POINTER", "advanced" if moved else "end of book")

    elif verb in ("meaning", "m"):
        if reading.state.is_meaning_mode:
            await reading.meaning_mode_off()
            dashboard.event("MEANING_MODE_OFF", "back to the page")
        else:
            await reading.meaning_mode_on()
            dashboard.event("MEANING_MODE_ON", "clock stopped, friction recorded")

    elif verb in ("lookup", "l"):
        word = parts[1] if len(parts) > 1 else ""
        reading.lookup_completed(word)
        dashboard.event("LOOKUP_COMPLETED", word or "word explained")

    elif verb in ("gesture", "g"):
        if len(parts) < 2:
            raise ValueError("gesture needs a sentence number, e.g. 'gesture 12'")
        index = int(parts[1])
        # "back" marks the reader dragging a wrong pointer into place. Ordinary
        # forward movement is not friction and must not be counted as any.
        corrected = len(parts) > 2 and parts[2] in ("back", "correct", "correction")
        target = ReadingPointer(
            page_index=reading.pointer.page_index,
            paragraph_index=reading.pointer.paragraph_index,
            sentence_index=index,
        )
        moved = await reading.gesture_to(target, corrected=corrected)
        if moved:
            dashboard.event(
                "POINTER_UPDATED",
                f"sentence {index}" + (" (correction)" if corrected else ""),
            )
        else:
            dashboard.event("GESTURE IGNORED", "camera is off - nothing is watching")

    elif verb in ("page", "pg"):
        turned = await reading.turn_page()
        dashboard.event(
            "PAGE_CHANGED",
            f"now on page {reading.pointer.page_index}" if turned else "already at the last page",
        )

    elif verb == "ocr":
        version, applied = await reading.ocr_refine()
        dashboard.event("CONTENT_UPDATED", f"v{version}, audio applied={applied}")

    elif verb == "stale":
        wrongly_applied = await reading.deliver_stale_frame(version=0)
        dashboard.event(
            "STALE FRAME",
            "WRONGLY APPLIED - bug" if wrongly_applied else "refused by both consumers",
        )

    elif verb == "tts":
        wants_on = len(parts) > 1 and parts[1] == "on"
        enabled = await reading.set_tts(enabled=wants_on)
        dashboard.event("TTS", "narrating" if enabled else "silent reading")

    elif verb == "camera":
        if len(parts) < 2 or parts[1] not in ("on", "off"):
            raise ValueError("camera needs 'on' or 'off', e.g. 'camera off'")
        wants_on = parts[1] == "on"
        active = await reading.set_camera(active=wants_on)
        dashboard.event("CAMERA", "streaming" if active else "off - gesture and OCR blind")

    elif verb in ("summary", "s"):
        summary = await reading.finish_session()
        dashboard.stop()
        _print_summary(summary)
        dashboard.event("SESSION_FINISHED", f"{summary.session_wpm:.0f} wpm")

    else:
        dashboard.event("UNKNOWN COMMAND", f"'{command}' - type 'help'")


def _run_interactive(args: argparse.Namespace) -> int:
    memory = FakeMergeMemory.from_book()
    speed = ReadingSpeedService()
    speed.calibrate(READER_ID, word_count=CALIBRATION_WORDS, elapsed_ms=CALIBRATION_MS)

    # Always wired, even without `--tts`. An Audio Engine cannot be built after the
    # session starts — it needs a provider and a sink — so leaving it out would make
    # `tts on` permanently dead rather than merely off, and the command would report
    # "silent reading" no matter how often it was typed. `--tts` therefore chooses
    # the starting state, not whether narration is available at all.
    tts = PlaybackEngine(
        provider=FakeSpeechProvider(), sink=NullAudioSink(), session_id="sim-live"
    )
    tts.set_profile(get_profile("normal"))

    reading = SimulatedReadingEngine(
        session_id="sim-live",
        reader_id=READER_ID,
        memory=memory,
        speed=speed,
        tts=tts,
        tts_enabled=args.tts,
    )

    console.banner("TALETRACE SIMULATOR", "interactive - you are the reader")
    console.field("Book", f"{BOOK_TITLE} - {memory.page_count} pages")
    console.field("Narration", "on" if args.tts else "off (silent reading)")
    print()

    async def session() -> int:
        await reading.start_session(profile=get_profile("normal"))
        return await _interactive(reading, Dashboard())

    try:
        return asyncio.run(session())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 0


# ══════════════════════════════════════════════════════════════════ output


def _print_summary(summary) -> None:
    """The end-of-session report.

    Per-page evidence is printed under each verdict because a difficulty rating
    without its reasons is not checkable — and the pair of pages this simulation
    is built around only makes sense when you can see why they differ.
    """

    if summary is None:
        return

    print()
    console.banner("SESSION SUMMARY")
    console.field("Baseline", f"{summary.baseline_wpm:.0f} wpm")
    console.field("Session pace", f"{summary.session_wpm:.0f} wpm")
    console.field("Words read", summary.words_read)
    console.field("Pages read", summary.pages_read)
    console.field("Reading time", f"{summary.reading_duration_ms / 1000:.0f}s  (excludes pauses)")
    console.field("Wall time", f"{summary.wall_duration_ms / 1000:.0f}s")
    console.field("Lookups", summary.lookup_count)
    console.field("Meaning requests", summary.meaning_requests)
    console.field("Narrated", "yes" if summary.tts_assisted else "no")
    if summary.suggested_baseline_wpm:
        console.field(
            "Suggested baseline",
            f"{summary.suggested_baseline_wpm:.0f} wpm  (not applied)",
        )

    if summary.pages:
        print()
        console.rule("-")
        for page in summary.pages:
            print(
                f"  page {page.page_index}  {page.difficulty.value.upper():<8}"
                f" {page.words:>4}w"
                f"  expected {page.expected_ms / 1000:>5.0f}s"
                f"  actual {page.actual_ms / 1000:>5.0f}s"
                f"  ({page.deviation_ratio:+.0%})"
            )
            for reason in page.evidence:
                # Folded for the console: analytics writes an em dash, and this
                # output is routinely piped or redirected on Windows.
                print(f"            - {console.safe(reason)}")
        console.rule("-")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TaleTrace simulator - the full pipeline without the hardware."
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true", help="Drive the session yourself."
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Auto mode at human speed instead of compressed time.",
    )
    parser.add_argument(
        "--tts", action="store_true", help="Narrate (interactive mode)."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show module event logs."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s"
    )

    if args.interactive:
        return _run_interactive(args)
    return _run_auto(args)


if __name__ == "__main__":
    sys.exit(main())
