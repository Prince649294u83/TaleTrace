"""The integrated runtime, on screen.

    python -m backend.demo.runtime_demo              # stubbed AI, compressed time
    python -m backend.demo.runtime_demo --live-ai    # real Groq calls
    python -m backend.demo.runtime_demo --tts        # with narration

The difference between this and `main_demo`: that one fakes Merge Memory and the
Reading Engine to show the product before the product existed. This one fakes
nothing downstream of the hardware. The chain below is the real chain —

    recorded frames ──► OcrPipeline ──► MergeMemory ──► ReadingEngine
                              │                              │
                        GesturePipeline ──events──►     ┌─────┴─────┬──────────┐
                                                        ▼           ▼          ▼
                                                 ReadingSpeed  PlaybackEngine AiBridge

— assembled by `ReadingRuntime.build`, which is what a route will hold. Only the
camera and the ESP32 are replaced, by word boxes computed from `book.txt` and by
a fingertip coordinate. The OCR provider replaying them is the real provider.

Why it exists as well as the test suite: the E2E tests assert ordering, which is
what they are good for and what no screen can show. This shows *state* — twenty
eight fields from eight modules, side by side, while a session runs. A pointer
that agrees with Reading Speed but not with the audio queue is one glance here
and a long afternoon in a debugger otherwise.

The AI Engine is stubbed by default and real with `--live-ai`. Both are worth
having: the stub makes the run deterministic and free, and the live path is the
only thing that proves the bridge's context assembly produces prompts a model
actually answers.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from backend.app.modules.ai_engine.models import AiCapabilityResponse, BookMetadata
from backend.app.modules.audio_engine.audio_profiles import get_profile
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.audio_engine.speech_provider import (
    FakeSpeechProvider,
    NullAudioSink,
)
from backend.app.modules.gesture_engine.selection_models import FingerPoint
from backend.app.modules.ocr.providers import JsonOcrProvider
from backend.app.modules.reading_engine.ai_bridge import AiBridge
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.demo import console
from backend.demo.dashboard import Dashboard

READER_ID = "runtime-reader"
SESSION_ID = "runtime-demo"
BOOK_TITLE = "The Lighthouse"

# Measured, not guessed. Difficulty analysis refuses to rate a page against a
# default baseline, and the pages are the point of the run.
CALIBRATION_WORDS = 180
CALIBRATION_MS = 60_000

# Recorded page geometry. Line pitch 30px, words 80px wide, six to a line — the
# same layout the E2E fixtures use, so a fingertip y-coordinate here means the
# same line it means there.
WORDS_PER_LINE = 6
LINE_PITCH = 30

# How the scripted reader crosses each page, as a fraction of baseline. Page two
# is slow with friction behind it, page three slow with none: the pair that shows
# difficulty inference is reading the friction and not just the clock.
PAGE_PACES = {1: 1.00, 2: 0.45, 3: 0.55}

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════ the fake hardware


def recorded_page(text: str, *, confidence: float = 0.95) -> list[dict]:
    """Word boxes for `text`, as a camera frame would have produced them.

    This is the whole of the hardware fake. Everything downstream receives
    exactly what it would receive from a real frame, because `JsonOcrProvider`
    replays these through the same pipeline a Vision response goes through.
    """

    out: list[dict] = []
    for index, word in enumerate(text.split()):
        column, row = index % WORDS_PER_LINE, index // WORDS_PER_LINE
        x, y = column * 90, row * LINE_PITCH
        out.append(
            {
                "text": word,
                "bbox": [x, y, x + 80, y + 24],
                "confidence": confidence,
                "word_index": index,
                "line_index": row,
            }
        )
    return out


def blank_frame(height: int = 400, width: int = 600):
    """A frame the finger detector will find nothing in.

    The fingertip is supplied directly, so the image only has to be the right
    shape. Detection itself is covered in `test_gesture_engine.py` — running
    MediaPipe here would make the demo need a camera to prove a point about
    wiring.
    """

    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a hard dependency of OCR
        return None
    return np.zeros((height, width, 3), dtype=np.uint8)


def load_pages(path: Path | None = None) -> list[str]:
    """The book, as pages of two paragraphs each.

    Same source file and same splitting rule as `FakeMergeMemory` — `---` divides
    chapters, blank lines divide paragraphs, two paragraphs to a page, and a page
    never straddles a chapter break. Matching it matters because a page number
    seen here has to mean the same page it means in the other demo.
    """

    source = path or Path(__file__).with_name("book.txt")
    raw = source.read_text(encoding="utf-8")

    pages: list[str] = []
    for section in [s for s in raw.split("\n---") if s.strip()]:
        blocks = [
            block.strip().replace("\n", " ")
            for block in section.strip().split("\n\n")
            if block.strip()
        ]
        for start in range(0, len(blocks), 2):
            pages.append("\n\n".join(blocks[start : start + 2]))
    return pages


class SimulatedClock:
    """A clock that moves only when the script says so.

    Injected into every module that measures time, which is what makes
    compressed mode honest rather than approximate: the arithmetic is the real
    arithmetic on a faster clock. Without it a three-page session finishing in
    40ms would report a reader at 400,000 wpm and every difficulty verdict would
    be noise.
    """

    def __init__(self, *, speedup: float = 1.0, real: bool = False) -> None:
        self._now = 0.0
        self._speedup = speedup
        self._real = real

    def __call__(self) -> float:
        return time.monotonic() if self._real else self._now

    async def sleep(self, seconds: float) -> None:
        if self._real:
            await asyncio.sleep(seconds / self._speedup)
        else:
            self._now += seconds


# ═══════════════════════════════════════════════════════════════ the AI stub


EXPLANATIONS = {
    "lighthouse": {
        "oled_text": "a tower with a light",
        "full_explanation": "A tall tower by the sea whose lamp warns ships away from rocks.",
        "difficulty_level": "beginner",
    },
    "physician": {
        "oled_text": "a doctor",
        "full_explanation": "An old word for a doctor — someone who treats people who are ill.",
        "difficulty_level": "intermediate",
    },
}

REVIEW = {
    "session_summary": (
        "Mira climbed the dark lighthouse, found the keeper's logbook, "
        "and relit the lamp for the village."
    ),
    "words_learned": [
        {"word": "lighthouse", "takeaway": "a tower that warns ships"},
        {"word": "physician", "takeaway": "an old word for a doctor"},
    ],
    "flashcards": [
        {"word": "lighthouse", "fun_definition": "a tower with a lamp that says 'rocks ahead!'"},
        {"word": "physician", "fun_definition": "a doctor from a long time ago"},
    ],
    "quiz": [
        {"question": "What warns ships away from rocks?", "correct_answer": "a lighthouse"},
        {"question": "What does a physician do?", "correct_answer": "treats people who are ill"},
    ],
}


class StubExplanationEngine:
    """Canned explanations, keyed on the word. Deterministic and free.

    Not a mock of the interface — it is the interface, with the network removed.
    The bridge cannot tell the difference, which is the point: what the session
    does with an answer is the same either way.
    """

    def explain(self, request):
        word = (request.context.selected_word or "").lower().strip(".,;:!?\"'")
        payload = EXPLANATIONS.get(word)
        if payload is None:
            payload = {
                "oled_text": f"about '{word}'",
                "full_explanation": f"'{word}' as it is used in this sentence.",
                "difficulty_level": "beginner",
            }
        return AiCapabilityResponse(
            status="ok", capability="explanation_engine", message="{}", data=dict(payload)
        )


class StubSummaryGenerator:
    def summarize(self, request):
        return AiCapabilityResponse(
            status="ok", capability="summary_generator", message="{}", data=dict(REVIEW)
        )


def build_ai(*, live: bool) -> AiBridge:
    """The bridge, with real engines or stubs behind it.

    `book` is set either way. It is what turns a bare word into a prompt that
    knows the reader is nine years old and the genre is adventure, and dropping
    it in the stubbed path would mean the live run exercised a code path the
    default run never touched.
    """

    ai = AiBridge(
        book=BookMetadata(
            title=BOOK_TITLE,
            author="A. Wren",
            genre="middle-grade adventure",
            chapter="Chapter 1",
            audience="ages 9-12",
        )
    )
    if not live:
        ai.explanation_engine = StubExplanationEngine()
        ai.summary_generator = StubSummaryGenerator()
    return ai


# ═══════════════════════════════════════════════════════════════════ the run


class RuntimeSimulation:
    """A scripted reader crossing the book through the assembled runtime.

    Every state change enters through `ReadingRuntime` — a frame, a fingertip, or
    a sentence advance. This class never writes a pointer, never calls Reading
    Speed, and never touches the Audio Engine, which is the constraint that makes
    the screen worth trusting: what it shows was produced by the same call
    sequence a real camera will make.
    """

    def __init__(
        self,
        runtime: ReadingRuntime,
        dashboard: Dashboard,
        clock: SimulatedClock,
        pages: list[str],
        *,
        audio: PlaybackEngine | None = None,
    ) -> None:
        self.runtime = runtime
        self.dashboard = dashboard
        self.clock = clock
        self.pages = pages
        self.audio = audio
        self.frame = blank_frame()
        self._fired: set[tuple[int, int]] = set()

    # ---------------------------------------------------------------- helpers

    @property
    def engine(self):
        return self.runtime.engine

    async def paint(self) -> None:
        """One frame of the monitor. Reads state; changes none.

        Every value is fetched fresh rather than cached, so the screen cannot
        show something the system has stopped believing.
        """

        speed = self.engine.speed
        session = self.engine.session_id
        pointer = self.engine.state.pointer

        # Frames arrive before the session opens — a reader points the camera at
        # the page before they start reading, and OCR should have a page ready by
        # the time they do. Reading Speed refuses to invent a tracker for a
        # session that has not begun, and it is right to: a pace measured from a
        # clock that started at the wrong moment looks plausible and is wrong. So
        # there is nothing to paint yet.
        if not speed.has(session):
            return

        self.dashboard.draw(
            state=self.engine.state,
            snapshot=speed.tracker(session).snapshot(),
            prediction=speed.predict(session),
            baseline=speed.baseline_for(READER_ID),
            observed_wpm=speed.observed_wpm(session),
            audio=self.audio.get_status() if self.audio is not None else None,
            difficulties=speed.page_difficulties(session),
            book_title=BOOK_TITLE,
            chapter=f"Chapter 1, page {pointer.page_index}",
            page_count=len(self.pages),
            sentence=self.sentence_text(),
            memory_version=self.engine.memory.version,
            ocr=self.runtime.last_frame,
            gesture=self.runtime.last_selection,
            explanation=self.engine.explanation,
            review=self.engine.review,
            session_seconds=self.clock(),
        )

    def sentence_text(self) -> str:
        """The text of the sentence the pointer names.

        Segmented fresh from Merge Memory using the Audio Engine's own segmenter,
        so if the pointer and the narration ever disagree about which sentence
        this is, the screen shows it instead of hiding it behind a cached string.
        """

        pointer = self.engine.state.pointer
        for chunk in segment_sentences(self.engine.current_text()):
            if chunk.pointer.sentence_index == pointer.sentence_index:
                return chunk.text
        return ""

    async def read_for(self, seconds: float) -> None:
        """Spend `seconds` reading, repainting about once a second."""

        remaining = seconds
        while remaining > 0:
            step = min(1.0, remaining)
            await self.clock.sleep(step)
            remaining -= step
            await self.paint()

    def fingertip_on_line(self, line: int, column: int = 0) -> FingerPoint:
        """A fingertip in the middle of one recorded word.

        Centred on the word in both axes, because the selector scores by
        distance and a point on a boundary is a coin flip between two
        neighbours — a flaky demo teaches the wrong lesson about the selector.

        `column` exists so a scripted gesture can point at a word worth
        explaining. The selector treats every word alike, so pointing at 'as'
        is as correct as pointing at 'lighthouse' and reads as though something
        misfired.
        """

        return FingerPoint(
            x=column * 90 + 40,
            y=line * LINE_PITCH + 12,
            confidence=0.95,
        )

    def once(self, key: tuple[int, int]) -> bool:
        """Whether a scripted beat should fire, once and only once.

        A gesture correction moves the pointer backwards, so the same sentence
        index comes round again. Without this the correction re-fires forever and
        the reader never leaves the page.
        """

        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        self.dashboard.event("CALIBRATED", f"180 wpm from a {CALIBRATION_WORDS}-word passage")

        # Page one arrives as two frames: a poor one, then a better one. That is
        # the normal case, not an error case — OCR refines a page as the reader
        # holds it, and Merge Memory's version is what everything downstream uses
        # to notice.
        await self.deliver_page(1, degraded=True)
        await self.engine.start_session(profile=get_profile("normal"))
        self.dashboard.event("SESSION_STARTED", "page 1")

        await self.deliver_page(1)
        self.dashboard.event("OCR_REFINED", f"memory v{self.engine.memory.version}")

        for page_number in range(1, min(len(self.pages), 4) + 1):
            if page_number > 1:
                await self.deliver_page(page_number)
                self.dashboard.event("PAGE_CHANGED", f"page {page_number}")
            await self.read_page(page_number)

        await self.read_for(2.0)

    async def deliver_page(self, page_number: int, *, degraded: bool = False) -> None:
        """Hand OCR a frame of page `page_number`.

        A degraded frame is the same words at lower confidence rather than
        different words: the pipeline's confidence floor is what decides whether
        to keep a frame, and feeding it nonsense would test the wrong gate.
        """

        text = self.pages[page_number - 1]
        words = recorded_page(text, confidence=0.55 if degraded else 0.95)
        result = await self.runtime.feed_camera_frame(words)

        if not result.accepted:
            self.dashboard.event("FRAME_IGNORED", result.reason)
        await self.paint()

    async def read_page(self, page_number: int) -> None:
        """Read every sentence Reading Speed knows about on this page."""

        wpm = 180.0 * PAGE_PACES.get(page_number, 1.0)
        content = self.engine.content

        spans = [s for s in content.sentences if s.pointer.page_index == page_number]
        for position, span in enumerate(spans):
            await self.read_for((span.word_count / wpm) * 60.0)
            await self.beat(page_number, position)

            # The pointer moves because the reader moved, not because time
            # passed: this is the sentence advance the real Gesture Engine will
            # cause when the fingertip crosses a line.
            await self.engine.move_pointer(span.pointer)

    async def beat(self, page: int, position: int) -> None:
        """The scripted incidents. One per page, each a different subsystem.

        Page 1  a pointing gesture, through the real gesture pipeline
        Page 2  a word lookup and a pointer correction — friction, with a cause
        Page 3  twenty seconds away from the book — the same slow pace, no cause
        Page 4  the camera goes dark and comes back
        """

        key = (page, position)

        if page == 1 and position == 1 and self.once(key):
            # A real selection: fingertip -> selector -> published event ->
            # drained into the engine. The pointer this moves was moved by
            # Gesture, not by this script.
            result = await self.runtime.feed_gesture_frame(
                self.frame, finger=self.fingertip_on_line(0, column=1)
            )
            word = result.selected_word or result.status.value
            self.dashboard.event("GESTURE", f"pointed at '{word}'")

        if page == 2 and position == 1 and self.once(key):
            word = self.current_lookup_word(prefer="lighthouse")
            self.dashboard.event("MEANING_REQUESTED", f"'{word}'")
            await self.engine.meaning_mode_on(word=word)
            await self.read_for(8.0)

            outcome = self.engine.explanation
            if outcome is not None and outcome.ok:
                self.dashboard.event("AI_EXPLAINED", outcome.oled_text)
            elif outcome is not None:
                self.dashboard.event("AI_FAILED", outcome.error)

            await self.engine.meaning_mode_off()
            self.dashboard.event("MEANING_MODE_OFF", "back to reading")

        if page == 2 and position == 3 and self.once(key):
            # The pointer was wrong and the reader drags it back. Marked as a
            # correction, which is what makes it friction evidence rather than
            # ordinary forward movement.
            #
            # Back to the *first sentence of this page*, not `sentences[0]` —
            # that is page one's opening line, and dragging the pointer onto a
            # page the reader is not holding would close page two early and put
            # the correction on the wrong page's evidence.
            here = [
                span.pointer
                for span in self.engine.content.sentences
                if span.pointer.page_index == page
            ]
            if here:
                target = here[0]
                self.dashboard.event("GESTURE", f"corrected back to s{target.sentence_index}")
                await self.engine.move_pointer(target, corrected=True)

        if page == 3 and position == 1 and self.once(key):
            self.dashboard.event("SESSION_PAUSED", "book set down (20s)")
            await self.engine.pause()
            await self.read_for(20.0)
            await self.engine.resume()
            self.dashboard.event("SESSION_RESUMED", "picked back up")

        if page == 4 and position == 1 and self.once(key):
            self.engine.camera_off("reader covered the lens")
            self.dashboard.event("CAMERA_OFF", "gesture and OCR blind")
            await self.read_for(4.0)
            # Recovered by a frame arriving, not by a call: the engine infers the
            # camera is back from the frame itself, which is the only evidence
            # that actually exists.
            await self.deliver_page(4)
            self.dashboard.event("CAMERA_ON", "frames resumed")

    def current_lookup_word(self, *, prefer: str = "") -> str:
        """A word from the sentence being read, preferring one with a canned answer.

        Taken from the sentence rather than picked in advance, because the point
        is that the word came from the text Merge Memory actually holds. Falls
        back to the longest word, which is the one a nine-year-old is most likely
        to stop on.
        """

        text = self.sentence_text() or ""
        words = [w.strip(".,;:!?\"'") for w in text.split()]
        words = [w for w in words if len(w) > 3]
        if not words:
            return prefer or "lighthouse"
        for word in words:
            if word.lower() == prefer.lower():
                return word
        return max(words, key=len)


# ══════════════════════════════════════════════════════════════════ reporting


def _print_summary(analytics, engine) -> None:
    console.rule()
    console.banner("SESSION SUMMARY", "measured by Reading Speed")
    console.field("Baseline", f"{analytics.baseline_wpm:.0f} wpm")
    console.field("Session pace", f"{analytics.session_wpm:.0f} wpm")
    console.field("Words read", analytics.words_read)
    console.field("Pages read", len(analytics.pages))
    console.field("Reading time", f"{analytics.reading_duration_ms / 1000:.0f}s  (excludes pauses)")
    console.field("Wall time", f"{analytics.wall_duration_ms / 1000:.0f}s")
    console.field("Lookups", analytics.lookup_count)
    console.field("Meaning requests", analytics.meaning_requests)
    print()

    review = engine.review
    if review is not None and review.ok:
        console.field("Summary", review.data.get("session_summary", ""))
        console.field("Flashcards", len(review.data.get("flashcards", []) or []))
        console.field("Quiz", len(review.data.get("quiz", []) or []))
        for card in review.data.get("flashcards", []) or []:
            console.event("flashcard", f"{card.get('word')}: {card.get('fun_definition', '')}")
        for question in review.data.get("quiz", []) or []:
            console.event("quiz", str(question.get("question", "")))
    elif review is not None:
        console.field("Review", f"unavailable - {review.error}")
    print()

    console.rule("-")
    for page in analytics.pages:
        print(
            f"  page {page.page_index}  {page.difficulty.value.upper():<9} "
            f"{page.words:>4}w  expected {page.expected_ms / 1000:>5.0f}s  "
            f"actual {page.actual_ms / 1000:>5.0f}s"
        )
        for line in page.evidence:
            # Through `_safe` because the evidence strings contain em dashes and
            # Windows terminals still default to cp1252 — an encode error here
            # would take out the summary of a run that worked.
            print(f"            - {console.safe(line)}")
    console.rule("-")


def _checks(analytics, engine, runtime) -> list[tuple[bool, str, str]]:
    """What this run has to demonstrate for the integration to count as working.

    Every check is cross-module on purpose. A single module behaving is a unit
    test's job; what can only fail here is two modules disagreeing.
    """

    pages = {page.page_index: page for page in analytics.pages}
    p2, p3 = pages.get(2), pages.get(3)
    version = engine.memory.version
    explanation = engine.explanation
    review = engine.review

    return [
        (
            engine.state.is_finished,
            "session closed through the engine",
            "state.is_finished",
        ),
        (
            version > 1,
            "OCR refined the page and Merge Memory versioned it",
            f"v{version}",
        ),
        (
            runtime.last_frame is not None and runtime.last_frame.accepted,
            "the last frame reached Merge Memory",
            f"{len(runtime.last_frame.words) if runtime.last_frame else 0} words",
        ),
        (
            runtime.last_selection is not None and runtime.last_selection.succeeded,
            "a real gesture selected a word",
            (runtime.last_selection.selected_word if runtime.last_selection else "none"),
        ),
        (
            runtime.pending == [],
            "every published gesture event was drained",
            f"{len(runtime.pending)} left",
        ),
        (
            analytics.words_read > 0,
            "Reading Speed counted words",
            f"{analytics.words_read} words",
        ),
        (
            analytics.reading_duration_ms < analytics.wall_duration_ms,
            "the reading clock excluded the pause",
            f"{analytics.reading_duration_ms / 1000:.0f}s reading "
            f"< {analytics.wall_duration_ms / 1000:.0f}s wall",
        ),
        (
            explanation is not None and explanation.ok,
            "the AI Engine answered a lookup",
            (explanation.oled_text if explanation is not None and explanation.ok
             else "no explanation"),
        ),
        (
            analytics.lookup_count >= 1,
            "the answered lookup reached Reading Speed",
            f"{analytics.lookup_count}",
        ),
        (
            review is not None and review.ok and bool(review.data.get("flashcards")),
            "the review produced flashcards",
            f"{len(review.data.get('flashcards', []) or []) if review and review.ok else 0}",
        ),
        (
            p2 is not None and p2.lookups >= 1 and p2.pointer_corrections >= 1,
            "the friction on page 2 was attributed to page 2",
            f"{p2.lookups} lookups, {p2.pointer_corrections} corrections" if p2 else "missing",
        ),
        (
            p3 is not None and p3.lookups == 0 and p3.pointer_corrections == 0,
            "the interrupted page carries no friction evidence",
            f"page 3 = {p3.difficulty.value}" if p3 else "page 3 missing",
        ),
    ]


# ═════════════════════════════════════════════════════════════════════ entry


async def _run(args: argparse.Namespace) -> int:
    pages = load_pages()
    clock = SimulatedClock(real=args.realtime)

    console.banner("TALETRACE RUNTIME", "the assembled chain, no hardware")
    console.field("Book", f"{BOOK_TITLE} - {len(pages)} pages")
    console.field("Clock", "real time" if args.realtime else "compressed")
    console.field("AI Engine", "live Groq" if args.live_ai else "stubbed (deterministic)")
    console.field("Narration", "on" if args.tts else "off")
    console.field("Script", "p1 gesture | p2 lookup+correction | p3 pause | p4 camera loss")
    print()

    if args.live_ai and not (os.environ.get("GROQ_API_KEY") or "").strip():
        console.field("Warning", "--live-ai given but GROQ_API_KEY is not set")
        print()

    speed = ReadingSpeedService(clock=clock)
    speed.calibrate(READER_ID, word_count=CALIBRATION_WORDS, elapsed_ms=CALIBRATION_MS)

    audio = None
    if args.tts:
        audio = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            session_id=SESSION_ID,
            clock=clock,
            auto_advance=False,
        )

    runtime = ReadingRuntime.build(
        session_id=SESSION_ID,
        reader_id=READER_ID,
        ocr_provider=JsonOcrProvider(),
        audio=audio,
        ai=build_ai(live=args.live_ai),
        book_id="book-lighthouse",
        clock=clock,
        speed=speed,
    )

    dashboard = Dashboard()
    simulation = RuntimeSimulation(runtime, dashboard, clock, pages, audio=audio)

    try:
        await simulation.run()
        # Through the engine, not `speed.finish_session()`: ending a session is a
        # session event, and routing around its owner would leave the state
        # saying the session is still open while the analytics say it closed.
        analytics = await runtime.engine.finish_session()
    finally:
        dashboard.stop()

    _print_summary(analytics, runtime.engine)
    return console.verdict(_checks(analytics, runtime.engine, runtime))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the assembled TaleTrace runtime.")
    parser.add_argument(
        "--realtime", action="store_true", help="run at human speed instead of compressed"
    )
    parser.add_argument(
        "--live-ai", action="store_true", help="call real Groq instead of the stub"
    )
    parser.add_argument("--tts", action="store_true", help="narrate through the Audio Engine")
    parser.add_argument("-v", "--verbose", action="store_true", help="show module logs")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
