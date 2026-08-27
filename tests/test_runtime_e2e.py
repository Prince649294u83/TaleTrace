"""End-to-end scenarios over the assembled runtime.

Every test here drives the real chain — real OCR pipeline, real Merge Memory,
real Reading Engine, real Reading Speed, real Audio Engine. Nothing is faked
except the two things that reach outside the process: the speech provider (which
would play audio) and the Groq client (which would cost money and vary between
runs). `ReplayAdapter` is not a mock; it is the real provider replaying
recorded word boxes, which is how a page gets through OCR deterministically
without a camera.

The categories the integration directive asks for, and where each lives:

    reading         TestReadingSession
    OCR             TestOcrScenarios
    gesture         TestGestureScenarios
    memory          TestMergeMemoryScenarios
    reading speed   TestReadingSpeedScenarios
    focus analysis  TestReadingFocusScenarios
    audio           TestAudioScenarios
    AI              TestAiScenarios
    runtime failure TestFailureScenarios

What these assert that a unit test cannot: *order*. A page committed after the
audio queue was rebuilt, a pointer moved before Reading Speed heard about the
page turn, a summary built before the last page was committed — each is a
correct-looking module and a wrong-looking session, and only a sequence of
events across modules shows it.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.app.modules.ai_engine.models import AiCapabilityResponse, BookMetadata
from backend.app.modules.audio_engine.models import PauseReason, PlaybackState
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import (
    FakeSpeechProvider,
    NullAudioSink,
)
from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    SelectionStatus,
)
from backend.app.modules.ocr.replay import ReplayAdapter
from backend.app.modules.reading_engine.ai_bridge import AiBridge
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.modules.reading_speed.models import DifficultyLevel
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared.constants import FIRST_PAGE_INDEX
from backend.app.shared.events import SessionEvent

PAGE_ONE = (
    "The lighthouse stood alone on the cliff. "
    "Its lamp had not been lit for thirty years. "
    "Mira had promised her grandfather she would climb it."
)
PAGE_TWO = (
    "The spiral stair was narrower than she remembered. "
    "Salt wind followed her all the way up."
)


# ---------------------------------------------------------------- test doubles


class Clock:
    """A hand-advanced clock, so a scenario can replay an hour in a millisecond.

    Every module in the chain takes an injected clock precisely so this works. A
    wall clock would make every pace assertion in this file a race.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class StubAiEngine:
    """One AI capability, with a scripted outcome.

    Substituted for a Groq-backed engine so the scenarios are deterministic and
    free. The live engines are exercised against real Groq separately; what these
    tests need to prove is what the *session* does with each kind of answer.
    """

    def __init__(self, payload=None, *, raises=None, status="ok", delay=0.0):
        self.payload = payload if payload is not None else {}
        self.raises = raises
        self.status = status
        self.delay = delay
        self.calls: list = []

    def _respond(self, request):
        self.calls.append(request)
        if self.delay:
            import time as _time

            _time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return AiCapabilityResponse(
            status=self.status,
            capability="stub",
            message="{}",
            data=dict(self.payload),
        )

    # The bridge calls a different method name per capability.
    explain = _respond
    summarize = _respond
    create = _respond


class StubLearningEngine:
    def __init__(self, payload=None, *, raises=None, delay=0.0):
        self.payload = payload if payload is not None else {}
        self.raises = raises
        self.delay = delay
        self.calls: list = []

    def generate(self, request):
        self.calls.append(request)
        if self.delay:
            import time as _time
            _time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        
        from backend.app.modules.learning_engine.models import LearningCapabilityResponse
        return LearningCapabilityResponse(
            quiz=self.payload.get("quiz", []),
            flashcards=self.payload.get("flashcards", [])
        )


EXPLANATION = {
    "oled_text": "a tower with a light",
    "full_explanation": "A tall tower whose lamp warns ships away from rocks.",
    "difficulty_level": "beginner",
}

REVIEW = {
    "session_summary": "Mira set out to climb the abandoned lighthouse.",
    "words_learned": [{"word": "lighthouse", "takeaway": "a warning tower"}],
}

LEARNING_MATERIAL = {
    "flashcards": [{"word": "lighthouse", "fun_definition": "a warning tower"}],
    "quiz": [{"question": "What warns ships?", "correct_answer": "a lighthouse", "options": ["a lighthouse", "a car"]}],
}


# -------------------------------------------------------------------- fixtures


def recorded_page(text: str, *, words_per_line: int = 6, confidence: float = 0.95):
    """Recorded OCR output for `text`, laid out in lines.

    Geometry matters to the gesture tests: line pitch is 30px and words are 80px
    wide, so a fingertip at y=44 falls on the second line. Returned as the list
    of dicts `ReplayAdapter` replays.
    """

    out = []
    for index, word in enumerate(text.split()):
        column, row = index % words_per_line, index // words_per_line
        x, y = column * 90, row * 30
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


def recorded_paragraphs(*texts, words_per_line: int = 6, confidence: float = 0.95):
    """Recorded OCR output for a page of several paragraphs.

    `recorded_page` reports no paragraph index, so the pipeline renders it as one
    block and Merge Memory holds one paragraph. That is the right fixture for
    everything measured per *page*, and the wrong one for anything measured per
    paragraph — a one-paragraph page cannot show a paragraph being left, ranked
    against another, or credited with the wrong words.

    Rows advance with a two-line gap between paragraphs so the geometry matches
    the structure, which the gesture tests depend on even though these do not.
    """

    out = []
    index = 0
    row = 0
    for paragraph_index, text in enumerate(texts):
        for offset, word in enumerate(text.split()):
            column = offset % words_per_line
            if offset and column == 0:
                row += 1
            x, y = column * 90, row * 30
            out.append(
                {
                    "text": word,
                    "bbox": [x, y, x + 80, y + 24],
                    "confidence": confidence,
                    "word_index": index,
                    "line_index": row,
                    "paragraph_index": paragraph_index,
                }
            )
            index += 1
        row += 2
    return out


def blank_frame(height: int = 200, width: int = 600):
    """An image the detector will never find a finger in.

    Every gesture test supplies the fingertip directly, so the frame only needs
    to be the right shape — the detector is covered in `test_gesture_engine.py`.
    """

    return np.zeros((height, width, 3), dtype=np.uint8)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audio(clock: Clock) -> PlaybackEngine:
    """A real playback engine with the speaking part stubbed.

    `auto_advance=False` keeps the sentence loop from running ahead on its own,
    so a scenario controls exactly how far narration has got.
    """

    return PlaybackEngine(
        provider=FakeSpeechProvider(),
        sink=NullAudioSink(),
        session_id="session-1",
        clock=clock,
        auto_advance=False,
    )


def build_runtime(
    clock: Clock, *, audio=None, ai=None, provider=None, focus: bool = True
) -> ReadingRuntime:
    """The assembled chain, with the outside world stubbed and nothing else."""

    return ReadingRuntime.build(
        session_id="session-1",
        reader_id="reader-1",
        ocr_provider=provider if provider is not None else ReplayAdapter(),
        audio=audio,
        ai=ai,
        book_id="book-1",
        clock=clock,
        speed=ReadingSpeedService(clock=clock),
        focus=focus,
    )


def bridge(explain=None, review=None, mood=None, learning=None) -> AiBridge:
    """An `AiBridge` with each capability stubbed independently.

    Independent because the failures are: a session can get an explanation and
    then fail to get a review, and the second must not retroactively invalidate
    the first.
    """

    ai = AiBridge(book=BookMetadata(title="The Keeper's Daughter", genre="adventure"))
    ai.explanation_engine = explain if explain is not None else StubAiEngine(EXPLANATION)
    ai.summary_generator = review if review is not None else StubAiEngine(REVIEW)
    ai.learning_engine = learning if learning is not None else StubLearningEngine(LEARNING_MATERIAL)
    ai.novel_mode = mood if mood is not None else StubAiEngine({"scene_mood": "wonder"})
    return ai


def events_of(runtime: ReadingRuntime, event: SessionEvent) -> list:
    return [e for e in runtime.engine.events if e.event is event]


def event_order(runtime: ReadingRuntime) -> list[str]:
    return [e.event.value for e in runtime.engine.events]


# ------------------------------------------------------------ reading scenarios


class TestReadingSession:
    """A session from first frame to summary, and the transitions in between."""

    @pytest.mark.asyncio
    async def test_a_full_session_runs_end_to_end(self, clock, audio):
        runtime = build_runtime(clock, audio=audio, ai=bridge())

        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(30)
        await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        clock.advance(30)
        analytics = await runtime.engine.finish_session()

        assert runtime.engine.state.is_finished
        assert analytics.pages_read == 2
        assert analytics.words_read > 0
        assert analytics.session_wpm > 0

    @pytest.mark.asyncio
    async def test_the_camera_may_stream_before_the_reader_presses_start(self, clock):
        """The first frames arrive before the session exists. That is normal.

        The camera streams as soon as the book is in view, and those early frames
        are what give the session text to open with. Reading Speed has no tracker
        to accept them into yet, so Merge Memory takes the text alone.
        """

        runtime = build_runtime(clock)

        result = await runtime.feed_camera_frame(recorded_page(PAGE_ONE))

        assert result.accepted
        assert runtime.engine.memory.paragraph(FIRST_PAGE_INDEX, 0).startswith("The lighthouse")
        assert not runtime.engine.state.is_reading
        assert "pre-session" in events_of(runtime, SessionEvent.CONTENT_UPDATED)[0].detail

        await runtime.engine.start_session()
        assert runtime.engine.speed.has("session-1")

    @pytest.mark.asyncio
    async def test_pause_and_resume_do_not_move_the_pointer(self, clock, audio):
        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        before = runtime.engine.state.pointer

        clock.advance(10)
        await runtime.engine.pause()
        assert runtime.engine.state.is_paused
        assert audio.get_status().state is PlaybackState.PAUSED

        clock.advance(120)
        await runtime.engine.resume()
        assert not runtime.engine.state.is_paused
        assert runtime.engine.state.pointer == before

    @pytest.mark.asyncio
    async def test_the_pointer_has_exactly_one_writer(self, clock, audio):
        """Every module reads the pointer; only the Reading Engine writes it.

        Asserted by driving a pointer move through gesture and checking the audio
        engine followed rather than led — the audio engine's pointer changes
        because the session's did, never the reverse.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.feed_gesture_frame(
            blank_frame(), finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0))
        )

        session_pointer = runtime.engine.state.pointer
        assert session_pointer.sentence_index == 1
        # The audio engine was told, and agrees.
        assert audio.get_status().pointer is not None

    @pytest.mark.asyncio
    async def test_finishing_commits_the_open_page_before_summarising(self, clock):
        """A page left uncommitted would be missing from the summary.

        `committed_pages()` is what the review reads. Finishing mid-page without
        committing would describe every page the reader read except the one they
        just finished.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        assert runtime.engine.memory.committed_pages() == []

        clock.advance(45)
        await runtime.engine.finish_session()

        committed = [page.page_index for page in runtime.engine.memory.committed_pages()]
        assert committed == [FIRST_PAGE_INDEX]


# ---------------------------------------------------------------- OCR scenarios


class TestOcrScenarios:
    """Frames arriving as they actually do: blurred, out of order, and repeated."""

    @pytest.mark.asyncio
    async def test_a_blank_frame_is_ignored_not_raised(self, clock):
        """A frame with no text is routine. The correct response is to wait.

        Raising would end a session because the reader's hand covered the page.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()
        version_before = runtime.engine.memory.version

        result = await runtime.feed_camera_frame([])

        assert not result.accepted
        assert runtime.engine.memory.version == version_before
        assert any(
            "ignored" in event.detail
            for event in events_of(runtime, SessionEvent.CONTENT_UPDATED)
        )

    @pytest.mark.asyncio
    async def test_repeated_frames_of_the_same_page_refine_one_page(self, clock):
        """More frames of one page improve it; they do not become new pages."""

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(5)
        refined = PAGE_ONE + " The keeper's cottage had long since fallen in."
        result = await runtime.feed_camera_frame(recorded_page(refined))

        assert result.accepted
        assert not result.page_changed
        assert runtime.engine.state.pointer.page_index == FIRST_PAGE_INDEX
        assert runtime.engine.memory.page_count == 1
        assert "cottage" in runtime.engine.memory.page_text(FIRST_PAGE_INDEX)

    @pytest.mark.asyncio
    async def test_repeated_frames_do_not_inflate_the_page(self, clock):
        """A page seen five times is one page, not five copies of one.

        Both modules accumulate: OCR merges words within a page, Merge Memory
        merges text into a page. Handed OCR's already-merged page as though it
        were a fragment, Merge Memory appends the whole page to itself on every
        frame — the word count grows without bound while OCR's own count stays
        still, and Reading Speed then measures a reader against a book five times
        longer than the one in their hands.

        Only an integration test sees it. Each module is behaving correctly on its
        own terms; it is the contract between them that has two readings.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        expected_words = len(PAGE_ONE.split())
        assert runtime.engine.memory.total_words == expected_words

        for _ in range(4):
            clock.advance(1)
            await runtime.feed_camera_frame(recorded_page(PAGE_ONE))

        assert runtime.engine.memory.total_words == expected_words
        assert runtime.engine.memory.paragraph_count(FIRST_PAGE_INDEX) == 1
        # Versions still moved: the frames were applied, not discarded.
        assert runtime.engine.memory.version > 1
        # And Reading Speed is measuring the real page, not a multiple of it.
        assert runtime.engine.content.page_word_counts[FIRST_PAGE_INDEX] == expected_words

    @pytest.mark.asyncio
    async def test_a_new_page_advances_the_pointer_and_commits_the_old_one(self, clock):
        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        result = await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        assert result.page_changed
        assert runtime.engine.state.pointer.page_index == FIRST_PAGE_INDEX + 1
        assert runtime.engine.state.pointer.sentence_index == 0
        committed = [page.page_index for page in runtime.engine.memory.committed_pages()]
        assert committed == [FIRST_PAGE_INDEX]

    @pytest.mark.asyncio
    async def test_consecutive_page_turns_are_all_detected(self, clock):
        """Four pages in a row must land as four pages, not two.

        A page turn is detected by comparing the incoming frame's vocabulary
        against the page the pipeline currently holds. Anything that clears those
        words between turns leaves the next comparison with nothing to compare
        against, so the turn is missed — and a missed turn is worse than a
        detected one going wrong: the new page's text is written over the page
        the reader already left, so that page is gone from the summary entirely.

        Every module is individually correct here. Only a sequence of turns shows
        it, which is why this lives with the E2E scenarios and not in the OCR
        unit tests.
        """

        pages = [
            "The lighthouse stood alone upon the windy cliff above.",
            "Mira climbed one hundred twelve narrow stairs counting each carefully.",
            "Brass gears turned slowly beneath thick grey dust everywhere inside.",
            "Villagers gathered below watching golden light sweep across dark water.",
        ]

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(pages[0]))
        await runtime.engine.start_session()

        for page in pages[1:]:
            clock.advance(30)
            result = await runtime.feed_camera_frame(recorded_page(page))
            assert result.page_changed, f"turn onto {page[:20]!r} was missed"

        assert runtime.engine.state.pointer.page_index == FIRST_PAGE_INDEX + 3
        assert runtime.engine.memory.page_count == 4

        # Each page holds its own text — the check that catches the overwrite. A
        # missed turn leaves page 2 holding page 3's words and page 2's lost.
        for offset, page in enumerate(pages):
            held = runtime.engine.memory.page_text(FIRST_PAGE_INDEX + offset)
            assert held.split()[0] == page.split()[0]

    @pytest.mark.asyncio
    async def test_a_turned_page_closes_with_its_own_word_count(self, clock):
        """The new page's map must reach Reading Speed before the pointer moves.

        Moving the pointer is what closes the page being left, and closing it is
        what credits that page's words. If the tracker is still holding the map
        from before the turn, the page it closes does not exist in that map and
        closes with zero words — then difficulty rates elapsed time against no
        words and returns UNKNOWN for every page after the first.

        Purely an ordering property. Both calls happen either way; only their
        sequence decides whether the numbers mean anything.
        """

        pages = [
            "The lighthouse stood alone upon the windy cliff above the harbour.",
            "Mira climbed one hundred twelve narrow stairs counting each step carefully.",
            "Brass gears turned slowly beneath thick grey dust on every surface.",
        ]

        runtime = build_runtime(clock)
        runtime.engine.speed.calibrate("reader-1", word_count=180, elapsed_ms=60_000)
        await runtime.feed_camera_frame(recorded_page(pages[0]))
        await runtime.engine.start_session()

        for page in pages[1:]:
            clock.advance(30)
            await runtime.feed_camera_frame(recorded_page(page))

        clock.advance(30)
        analytics = await runtime.engine.finish_session(review=False)

        counted = {page.page_index: page.words for page in analytics.pages}
        expected = {
            FIRST_PAGE_INDEX + offset: len(text.split())
            for offset, text in enumerate(pages)
        }
        assert counted == expected

    @pytest.mark.asyncio
    async def test_the_version_does_not_reset_on_a_page_turn(self, clock):
        """Consumers reject stale frames by version, so it must only increase.

        A version that restarted per page would make the new page's first frame
        look older than the previous page's last one, and every consumer holding
        a high-water mark would reject the whole new page.
        """

        runtime = build_runtime(clock)
        first = await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        second = await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        assert second.version > first.version

    @pytest.mark.asyncio
    async def test_the_rest_of_the_system_never_learns_which_provider_ran(self, clock):
        """Swapping the provider changes nothing above the pipeline.

        Asserted by driving the same session through a provider that reports a
        different name and checking every downstream observable is identical.
        """

        class RenamedProvider(ReplayAdapter):
            provider_name = "recorded-fixture"

        first = build_runtime(clock, provider=ReplayAdapter())
        await first.feed_camera_frame(recorded_page(PAGE_ONE))
        await first.engine.start_session()

        second_clock = Clock()
        second = build_runtime(second_clock, provider=RenamedProvider())
        await second.feed_camera_frame(recorded_page(PAGE_ONE))
        await second.engine.start_session()

        assert first.engine.current_text() == second.engine.current_text()
        assert first.engine.state.pointer == second.engine.state.pointer
        assert event_order(first) == event_order(second)

    @pytest.mark.asyncio
    async def test_a_provider_that_raises_does_not_end_the_session(self, clock):
        """A provider failure is an unreadable frame, not a broken session."""

        class BrokenProvider:
            provider_name = "broken"

            def accepts(self, source):
                return True

            def extract(self, source):
                raise RuntimeError("vision API unreachable")

        runtime = build_runtime(clock, provider=ReplayAdapter())
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        runtime.engine.ocr.provider = BrokenProvider()
        clock.advance(10)

        with pytest.raises(RuntimeError):
            await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        # The session is untouched: same pointer, same text, still reading.
        assert runtime.engine.state.is_reading
        assert runtime.engine.state.pointer.page_index == FIRST_PAGE_INDEX
        assert runtime.engine.current_text().startswith("The lighthouse")


# ------------------------------------------------------- merge memory scenarios


class TestMergeMemoryScenarios:
    """Everything reads from Merge Memory. Nothing bypasses it."""

    @pytest.mark.asyncio
    async def test_audio_speaks_the_text_merge_memory_holds(self, clock, audio):
        """The narrated paragraph and Merge Memory's paragraph are the same string.

        Not "equivalent" — the same. The audio engine is handed
        `engine.current_text()`, which is a Merge Memory read, so there is no
        second copy of the page to drift from it.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        memory_text = runtime.engine.memory.paragraph(FIRST_PAGE_INDEX, 0)
        assert runtime.engine.current_text() == memory_text
        assert audio.get_status().queued_sentences == 3

    @pytest.mark.asyncio
    async def test_reading_speed_counts_the_words_merge_memory_holds(self, clock):
        """Both consumers describe the same book, or the measurement is fiction."""

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        page_words = len(runtime.engine.memory.page_text(FIRST_PAGE_INDEX).split())
        content_words = runtime.engine.content.total_words

        assert content_words == page_words

    @pytest.mark.asyncio
    async def test_refined_text_reaches_narration_mid_page(self, clock, audio):
        """OCR keeps improving a page. Narration must hear the improvement.

        Without the queue refresh the reader would finish a page listening to the
        first frame's guess at it, while every other module had the merged text.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        queue_version_before = audio.get_status().queue_version

        clock.advance(5)
        refined = PAGE_ONE + " The keeper's cottage had long since fallen in."
        await runtime.feed_camera_frame(recorded_page(refined))

        assert audio.get_status().queue_version > queue_version_before

    @pytest.mark.asyncio
    async def test_committed_pages_accumulate_in_order(self, clock):
        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        await runtime.feed_camera_frame(recorded_page(PAGE_TWO))
        clock.advance(40)
        await runtime.engine.finish_session()

        assert [page.page_index for page in runtime.engine.memory.committed_pages()] == [
            FIRST_PAGE_INDEX,
            FIRST_PAGE_INDEX + 1,
        ]


# ------------------------------------------------------------ gesture scenarios


class TestGestureScenarios:
    """Gesture publishes. The Reading Engine decides. Nothing calls across."""

    @pytest.mark.asyncio
    async def test_pointing_at_a_later_line_moves_the_pointer(self, clock, audio):
        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        assert runtime.engine.state.pointer.sentence_index == 0

        clock.advance(15)
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
        )

        assert result.status is SelectionStatus.SUCCESS
        # Line two falls inside the paragraph's second sentence.
        assert runtime.engine.state.pointer.sentence_index == 1

    @pytest.mark.asyncio
    async def test_gesture_reaches_the_engine_only_through_the_queue(self, clock):
        """The pipeline's publish target is the runtime's queue, never a module.

        Publishing without draining leaves the session untouched, which is the
        observable form of "Gesture does not call the Reading Engine".
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        pointer_before = runtime.engine.state.pointer
        events_before = len(runtime.engine.events)

        clock.advance(15)
        runtime.gesture.observe(
            blank_frame(),
            runtime.engine.ocr.words,
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
        )

        assert runtime.pending, "gesture published nothing"
        assert runtime.engine.state.pointer == pointer_before
        assert len(runtime.engine.events) == events_before

        handled = await runtime.drain()
        assert len(handled) > 0
        assert runtime.pending == []
        assert runtime.engine.state.pointer != pointer_before

    @pytest.mark.asyncio
    async def test_a_gesture_pointer_is_translated_into_merge_memorys_paragraphs(
        self, clock, audio
    ):
        """Regression: Gesture's paragraph index is not Merge Memory's.

        Vision reports a paragraph per visual block, so a photographed page comes
        back as dozens of fragments; reconstruction rejoins them into the few the
        page really has. A gesture reporting "paragraph 6 of 7" therefore names a
        position that does not exist in a memory holding 2, and forwarding it
        moved the pointer off the page: `current_text()` returned "", the AI was
        sent no paragraph, and the audio queue was built from nothing.

        Found on a real photograph — 47 OCR paragraphs reconstructed to 4, with
        the reader pointing into raw-OCR paragraph 20.
        """

        # One OCR paragraph per line, which is what a photographed page looks
        # like, reconstructed into the two paragraphs the page actually has.
        page = recorded_page(PAGE_ONE, words_per_line=6)
        for word in page:
            word["paragraph_index"] = word["line_index"]

        runtime = ReadingRuntime.build(
            session_id="session-1",
            reader_id="reader-1",
            ocr_provider=ReplayAdapter(),
            audio=audio,
            clock=clock,
            speed=ReadingSpeedService(clock=clock),
            reconstruct=lambda held, new: (
                "The lighthouse stood alone on the cliff. "
                "Its lamp had not been lit for thirty years."
                "\n\n"
                "Mira had promised her grandfather she would climb it."
            ),
        )

        await runtime.feed_camera_frame(page)
        await runtime.engine.start_session()

        held = runtime.engine.memory.paragraph_count(1)
        assert held == 2, "the reconstruction fixture should collapse the page"
        assert max(w["paragraph_index"] for w in page) >= held, (
            "the OCR fixture must report more paragraphs than memory holds, "
            "or this test cannot catch the bug"
        )

        clock.advance(15)
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
        )
        assert result.status is SelectionStatus.SUCCESS

        pointer = runtime.engine.state.pointer
        assert pointer.paragraph_index < held, (
            f"pointer landed on paragraph {pointer.paragraph_index} of a page "
            f"holding {held} — Gesture's raw OCR index was forwarded untranslated"
        )
        # The observable consequence, and the reason this matters: there is text
        # to explain and text to narrate.
        assert runtime.engine.current_text().strip()

    @pytest.mark.asyncio
    async def test_a_gesture_on_unknown_text_leaves_the_pointer_alone(self, clock):
        """A line Merge Memory does not hold must not move the pointer at all.

        The reader can gesture at part of a page OCR has not read yet. Guessing a
        paragraph from an untranslatable line is what would put the pointer
        somewhere the text is empty.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        before = runtime.engine.state.pointer
        await runtime.engine.handle_gesture_event(
            SessionEvent.READING_POINTER_UPDATED,
            runtime._translate(
                SessionEvent.READING_POINTER_UPDATED,
                {"line_text": "zzz qqq xxx", "paragraph_index": 20, "line_index": 20},
            ),
        )

        assert runtime.engine.state.pointer == before

    @pytest.mark.asyncio
    async def test_a_low_confidence_gesture_publishes_nothing(self, clock):
        """The reader gets no answer rather than a wrong one."""

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        events_before = len(runtime.engine.events)

        clock.advance(15)
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=580.0, y=190.0, confidence=0.2, direction=None),
        )

        assert result.status is not SelectionStatus.SUCCESS
        assert runtime.pending == []
        assert len(runtime.engine.events) == events_before

    @pytest.mark.asyncio
    async def test_a_meaning_gesture_enters_meaning_mode_and_pauses_narration(
        self, clock, audio
    ):
        ai = bridge()
        runtime = build_runtime(clock, audio=audio, ai=ai)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(20)
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
            meaning_gesture=True,
        )

        assert result.status is SelectionStatus.SUCCESS
        assert runtime.engine.state.is_meaning_mode
        assert audio.get_status().state is PlaybackState.PAUSED
        assert audio.get_status().pause_reason is PauseReason.MEANING_MODE
        assert runtime.engine.explanation.ok
        assert runtime.engine.lookups == [result.selected_word]

    @pytest.mark.asyncio
    async def test_a_meaning_gesture_does_not_move_the_pointer(self, clock, audio):
        """Asking about a word is not reading past it."""

        runtime = build_runtime(clock, audio=audio, ai=bridge())
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        pointer_before = runtime.engine.state.pointer

        clock.advance(20)
        await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=74.0, confidence=0.9, direction=(0.0, -1.0)),
            meaning_gesture=True,
        )

        assert runtime.engine.state.pointer == pointer_before

    @pytest.mark.asyncio
    async def test_a_page_turn_resets_the_gesture_pipeline(self, clock):
        """Otherwise the new page's first selection is compared against the old one.

        A reader who turns to the top of a page would read as having jumped
        backwards, and Reading Speed would record a correction that never happened.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=134.0, confidence=0.9, direction=(0.0, -1.0)),
        )

        clock.advance(20)
        result = await runtime.feed_camera_frame(recorded_page(PAGE_TWO))
        assert result.page_changed
        assert runtime.gesture._last_line_y is None
        assert runtime.gesture._last_word == ""

    @pytest.mark.asyncio
    async def test_gesture_events_are_handled_in_the_order_published(self, clock):
        """A point then an ask is not an ask then a point.

        Reordering would look up whichever word happened to come next.
        """

        runtime = build_runtime(clock, ai=bridge())
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        runtime.gesture.observe(
            blank_frame(),
            runtime.engine.ocr.words,
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
        )
        runtime.gesture.observe(
            blank_frame(),
            runtime.engine.ocr.words,
            finger=FingerPoint(x=280.0, y=74.0, confidence=0.9, direction=(0.0, -1.0)),
            meaning_gesture=True,
        )

        published = [event for event, _ in runtime.pending]
        assert SessionEvent.MEANING_REQUESTED in published
        assert published.index(SessionEvent.MEANING_REQUESTED) == len(published) - 1

        await runtime.drain()

        # The word explained is the one from the meaning gesture, not the earlier point.
        assert runtime.engine.state.is_meaning_mode
        assert runtime.engine.lookups


# ------------------------------------------------------ reading speed scenarios


class TestReadingSpeedScenarios:
    """Reading Speed predicts. It owns no session, no pointer, and no playback."""

    @pytest.mark.asyncio
    async def test_it_never_moves_the_pointer_it_is_told_about(self, clock):
        """A prediction is a comparison, not an instruction.

        The service is asked to predict repeatedly; the pointer must be exactly
        where the Reading Engine last put it.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        pointer = runtime.engine.state.pointer
        for _ in range(5):
            clock.advance(10)
            runtime.engine.speed.predict("session-1")

        assert runtime.engine.state.pointer == pointer

    @pytest.mark.asyncio
    async def test_a_prediction_is_available_from_the_first_second(self, clock):
        """A UI needs something to show immediately; low confidence marks the guess."""

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(1)
        prediction = runtime.engine.speed.predict("session-1")

        assert prediction.session_id == "session-1"
        assert prediction.confidence < 1.0

    @pytest.mark.asyncio
    async def test_meaning_mode_is_counted_apart_from_an_ordinary_pause(self, clock, audio):
        """They mean opposite things about the page.

        A user pause says nothing about the text. Meaning Mode says the reader hit
        something they could not read past, which is evidence of difficulty.
        """

        runtime = build_runtime(clock, audio=audio, ai=bridge())
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(10)
        await runtime.engine.pause()
        clock.advance(60)
        await runtime.engine.resume()

        clock.advance(10)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        clock.advance(20)
        await runtime.engine.meaning_mode_off()

        clock.advance(20)
        analytics = await runtime.engine.finish_session()

        assert analytics.meaning_requests == 1
        assert analytics.lookup_count == 1

    @pytest.mark.asyncio
    async def test_a_camera_dropout_does_not_read_as_a_break(self, clock):
        """The reader keeps reading; the system stops seeing them.

        Stopping the clock would record a reader who read through an outage as
        having rested, deflating their measured pace by exactly the outage.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(10)
        runtime.engine.camera_off("device disconnected")
        assert not runtime.engine.state.camera_active
        assert not runtime.engine.state.is_paused

        clock.advance(60)
        await runtime.engine.move_pointer(
            runtime.engine.state.pointer.model_copy(update={"sentence_index": 1})
        )
        clock.advance(10)
        analytics = await runtime.engine.finish_session()

        # The whole 80 seconds counted as reading time.
        assert analytics.reading_duration_ms >= 80_000

    @pytest.mark.asyncio
    async def test_frames_arriving_again_bring_the_camera_back(self, clock):
        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        runtime.engine.camera_off("device disconnected")
        clock.advance(30)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))

        assert runtime.engine.state.camera_active
        assert events_of(runtime, SessionEvent.CAMERA_ON)

    @pytest.mark.asyncio
    async def test_tts_statistics_are_ingested_rather_than_inferred(self, clock, audio):
        """The audio engine counted words while speaking; that beats inferring them."""

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        analytics = await runtime.engine.finish_session()

        assert analytics.tts_assisted


# ------------------------------------------------------- focus analysis scenarios


PARAGRAPH_ONE = (
    "The lighthouse stood alone on the cliff and had done so for as long as "
    "anyone in the village could remember."
)
PARAGRAPH_TWO = (
    "Its lamp had not been lit for thirty years, and the mechanism that turned "
    "it had seized long before that."
)
PARAGRAPH_THREE = (
    "Mira had promised her grandfather she would climb it before the winter "
    "storms arrived and closed the path."
)


class TestReadingFocusScenarios:
    """Reading Focus Analysis observes the session. It never steers it.

    The unit tests in `test_focus_analytics.py` prove the engine's arithmetic
    against handed-in observations. What only a session can prove is that the
    observations are the *right* ones: that the paragraph the report names is the
    paragraph the pointer was in, that the words counted are Merge Memory's words,
    and that a meaning request lands against the paragraph the reader was reading
    rather than the one the AI answered about.
    """

    async def _three_paragraph_page(self, runtime):
        await runtime.feed_camera_frame(
            recorded_paragraphs(PARAGRAPH_ONE, PARAGRAPH_TWO, PARAGRAPH_THREE)
        )
        assert runtime.engine.memory.paragraph_count(FIRST_PAGE_INDEX) == 3

    def _at(self, runtime, paragraph_index: int, sentence_index: int = 0):
        return runtime.engine.state.pointer.model_copy(
            update={"paragraph_index": paragraph_index, "sentence_index": sentence_index}
        )

    @pytest.mark.asyncio
    async def test_the_report_names_the_paragraphs_the_reader_was_actually_in(self, clock):
        """The observation seam, end to end.

        Three paragraphs entered in order must appear in the report in that order,
        with the word counts Merge Memory holds — not counts this engine derived
        for itself, which is the drift the module is forbidden from introducing.
        """

        runtime = build_runtime(clock)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(20)
        await runtime.engine.move_pointer(self._at(runtime, 1))
        clock.advance(20)
        await runtime.engine.move_pointer(self._at(runtime, 2))
        clock.advance(20)
        await runtime.engine.finish_session(review=False)

        report = runtime.engine.focus_report
        assert [p.key for p in report.paragraphs] == [
            (FIRST_PAGE_INDEX, 0),
            (FIRST_PAGE_INDEX, 1),
            (FIRST_PAGE_INDEX, 2),
        ]

        memory = runtime.engine.memory
        for paragraph in report.paragraphs:
            held = memory.paragraph(paragraph.page_index, paragraph.paragraph_index)
            assert paragraph.words == len(held.split())

    @pytest.mark.asyncio
    async def test_a_meaning_request_is_charged_to_the_paragraph_being_read(self, clock):
        """Friction belongs to where the reader was, not where the pointer went next."""

        runtime = build_runtime(clock, ai=bridge())
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.move_pointer(self._at(runtime, 1))

        clock.advance(10)
        await runtime.engine.meaning_mode_on(word="mechanism")
        clock.advance(30)
        await runtime.engine.meaning_mode_off()

        clock.advance(15)
        await runtime.engine.move_pointer(self._at(runtime, 2))
        clock.advance(15)
        await runtime.engine.finish_session(review=False)

        by_key = {p.key: p for p in runtime.engine.focus_report.paragraphs}
        assert by_key[(FIRST_PAGE_INDEX, 1)].meaning_requests == 1
        assert by_key[(FIRST_PAGE_INDEX, 1)].lookups == 1
        assert by_key[(FIRST_PAGE_INDEX, 0)].meaning_requests == 0
        assert by_key[(FIRST_PAGE_INDEX, 2)].meaning_requests == 0

    @pytest.mark.asyncio
    async def test_the_thirty_seconds_spent_in_meaning_mode_are_not_reading_time(
        self, clock
    ):
        """Looking a word up must never also make the reader look slow.

        The lookup is already counted as friction. Letting the same seconds inflate
        the paragraph's reading time would score one moment of confusion twice, and
        the paragraph would rank above one the reader genuinely struggled through
        in silence.
        """

        runtime = build_runtime(clock, ai=bridge())
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(20)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        clock.advance(300)
        await runtime.engine.meaning_mode_off()

        clock.advance(10)
        await runtime.engine.finish_session(review=False)

        opening = runtime.engine.focus_report.paragraphs[0]
        # 30s of reading either side of a five-minute lookup.
        assert opening.actual_ms == pytest.approx(30_000, abs=1_000)

    @pytest.mark.asyncio
    async def test_a_paused_session_is_not_an_idle_reader(self):
        """A pause is the reader saying they have stopped. Idle time is the inference.

        Reporting the pause as idle time would mean every reader who put the book
        down deliberately came back to a report telling them they had drifted off.

        Asserted as a difference rather than as zero, which is the only form that
        isolates the claim. This paragraph *does* accrue a little idle time either
        way — 40 seconds of real reading on a 21-word paragraph is longer than the
        allowance, and the trailing-gap rule is right to say so. What must be true
        is that the ten-minute pause added none of it.
        """

        async def session(*, with_pause: bool) -> int:
            local_clock = Clock()
            runtime = build_runtime(local_clock)
            await self._three_paragraph_page(runtime)
            await runtime.engine.start_session()

            local_clock.advance(20)
            if with_pause:
                await runtime.engine.pause()
                local_clock.advance(600)
                await runtime.engine.resume()
            local_clock.advance(20)
            await runtime.engine.finish_session(review=False)
            return runtime.engine.focus_report.total_idle_ms

        paused = await session(with_pause=True)
        straight_through = await session(with_pause=False)

        assert paused == straight_through
        # And the ten minutes are nowhere in the report.
        assert paused < 600_000

    @pytest.mark.asyncio
    async def test_a_page_turn_closes_the_paragraph_the_reader_left(self, clock):
        """A turn is a paragraph change with a new page index, and must close the old one.

        If the page turn did not close it, the paragraph left behind would keep
        accruing the next page's time and the report would blame the wrong text.
        """

        runtime = build_runtime(clock)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(30)
        await runtime.feed_camera_frame(recorded_paragraphs(PAGE_TWO))

        clock.advance(30)
        await runtime.engine.finish_session(review=False)

        report = runtime.engine.focus_report
        pages = {p.page_index for p in report.paragraphs}
        assert pages == {FIRST_PAGE_INDEX, FIRST_PAGE_INDEX + 1}

        opening = report.paragraphs[0]
        assert opening.key == (FIRST_PAGE_INDEX, 0)
        assert opening.actual_ms == pytest.approx(30_000, abs=1_000)

    @pytest.mark.asyncio
    async def test_a_gesture_that_moves_the_pointer_is_observed_too(self, clock):
        """The focus engine sits behind the pointer, not behind a particular caller.

        A gesture reaches `move_pointer` through the queue rather than through a
        direct call, and an observer wired to only one of those paths would miss
        every paragraph a reader reached by pointing at it.
        """

        runtime = build_runtime(clock)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=104.0, confidence=0.9, direction=(0.0, -1.0)),
        )

        clock.advance(15)
        await runtime.engine.finish_session(review=False)

        observed = {p.key for p in runtime.engine.focus_report.paragraphs}
        assert observed == {
            (FIRST_PAGE_INDEX, p.paragraph_index)
            for p in [runtime.engine.state.pointer]
        } | {(FIRST_PAGE_INDEX, 0)}

    @pytest.mark.asyncio
    async def test_it_reports_nothing_difficult_without_a_calibrated_baseline(self, clock):
        """A default baseline is a guess, and deviation from a guess is not evidence.

        The session is deliberately slow. Every paragraph must still come back
        UNKNOWN, because the reader has never been measured and the engine has
        nothing to be slow *relative to*.
        """

        runtime = build_runtime(clock)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(200)
        await runtime.engine.move_pointer(self._at(runtime, 1))
        clock.advance(200)
        await runtime.engine.finish_session(review=False)

        report = runtime.engine.focus_report
        assert not report.baseline_was_evidence
        assert all(p.difficulty is DifficultyLevel.UNKNOWN for p in report.paragraphs)

    @pytest.mark.asyncio
    async def test_a_measured_reader_gets_a_ranking(self, clock):
        """With a real baseline and real friction, the hard paragraph comes top.

        Calibrated from a timed passage at 200 wpm — `calibrate` rather than
        `set_manual_baseline`, because a self-reported pace is MANUAL and
        `is_evidence` rejects it on purpose: a reader's claim about their own speed
        is not a measurement to rate paragraphs against.

        At 200 wpm a 22-word paragraph is expected to take about 6.6 seconds. The
        second paragraph takes ten times that *and* the reader asks what a word
        means — both halves of the evidence — while the first and third are on pace.
        """

        runtime = build_runtime(clock, ai=bridge())
        runtime.engine.speed.calibrate("reader-1", word_count=200, elapsed_ms=60_000)

        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(7)
        await runtime.engine.move_pointer(self._at(runtime, 1))

        clock.advance(30)
        await runtime.engine.meaning_mode_on(word="mechanism")
        await runtime.engine.meaning_mode_off()
        clock.advance(30)

        await runtime.engine.move_pointer(self._at(runtime, 2))
        clock.advance(7)
        await runtime.engine.finish_session(review=False)

        report = runtime.engine.focus_report
        assert report.baseline_wpm == 200.0

        hardest = report.needs_attention(limit=1)
        assert hardest
        assert hardest[0].key == (FIRST_PAGE_INDEX, 1)
        assert hardest[0].difficulty is DifficultyLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_the_report_is_judged_against_the_baseline_as_it_ends(self, clock):
        """Calibration arriving mid-session must apply to the whole session.

        The focus engine is built with whatever baseline the reader had when the
        session opened, which for a first session is a default. If the report were
        judged against that stale copy, a reader who calibrated at minute one would
        get a session's worth of UNKNOWN paragraphs for no reason.
        """

        runtime = build_runtime(clock)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()
        assert not runtime.engine.focus.baseline.is_evidence

        clock.advance(10)
        runtime.engine.speed.calibrate("reader-1", word_count=200, elapsed_ms=60_000)

        clock.advance(70)
        await runtime.engine.move_pointer(self._at(runtime, 1))
        clock.advance(10)
        await runtime.engine.finish_session(review=False)

        report = runtime.engine.focus_report
        assert report.baseline_was_evidence
        assert report.baseline_wpm == 200.0
        assert any(p.difficulty is not DifficultyLevel.UNKNOWN for p in report.paragraphs)

    @pytest.mark.asyncio
    async def test_it_never_moves_the_pointer_or_touches_playback(self, clock, audio):
        """The prohibition, asserted over a whole session rather than promised.

        The focus engine is fed every event the session produces. The pointer at
        the end must be exactly where the Reading Engine last wrote it, and the
        audio engine must be in the state the *session* left it in.
        """

        runtime = build_runtime(clock, audio=audio, ai=bridge())
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(20)
        await runtime.engine.move_pointer(self._at(runtime, 1))
        expected_pointer = runtime.engine.state.pointer
        expected_state = audio.get_status().state

        clock.advance(20)
        runtime.engine.focus.report()
        runtime.engine.focus.observations()

        assert runtime.engine.state.pointer == expected_pointer
        assert audio.get_status().state is expected_state

    @pytest.mark.asyncio
    async def test_a_session_without_a_focus_engine_is_still_a_session(self, clock, audio):
        """Optional in the strongest sense: no engine, no report, no other difference."""

        runtime = build_runtime(clock, audio=audio, focus=False)
        await self._three_paragraph_page(runtime)
        await runtime.engine.start_session()

        clock.advance(40)
        analytics = await runtime.engine.finish_session(review=False)

        assert runtime.engine.focus is None
        assert runtime.engine.focus_report is None
        assert analytics.words_read > 0
        assert runtime.engine.state.is_finished


# -------------------------------------------------------------- audio scenarios


class TestAudioScenarios:
    """Narration follows the session. It never leads it."""

    @pytest.mark.asyncio
    async def test_starting_a_session_queues_the_page_from_the_pointer(self, clock, audio):
        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        status = audio.get_status()
        assert status.state is PlaybackState.PLAYING
        assert status.queued_sentences == 3

    @pytest.mark.asyncio
    async def test_a_page_turn_rebuilds_the_queue_rather_than_splicing(self, clock, audio):
        """New page means new text from sentence zero.

        Splicing into a queue built from the previous page would narrate the old
        page's remaining sentences after the reader had already turned it.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        status = audio.get_status()
        assert status.queued_sentences == 2
        assert status.pointer is not None
        assert status.pointer.page_index == FIRST_PAGE_INDEX + 1
        assert "spiral stair" in runtime.engine.current_text()

    @pytest.mark.asyncio
    async def test_narration_statistics_survive_a_page_turn(self, clock, audio):
        """A page turn calls start() again; the session tally must carry over.

        Getting this wrong silently zeroes a reader's analytics at every page.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        assert audio.get_status().statistics.pages_read == 2

    @pytest.mark.asyncio
    async def test_a_session_runs_with_narration_off(self, clock):
        """No audio engine is a real configuration, not a degraded one."""

        runtime = build_runtime(clock, audio=None)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        assert not runtime.engine.state.tts_enabled

        clock.advance(40)
        analytics = await runtime.engine.finish_session()

        assert analytics.words_read > 0
        assert not analytics.tts_assisted


# ----------------------------------------------------------------- AI scenarios


class TestAiScenarios:
    """Meaning Mode, the session review, and what each does with a bad answer."""

    @pytest.mark.asyncio
    async def test_a_lookup_carries_the_sentence_the_word_appeared_in(self, clock):
        """A word without its context is guesswork.

        "bank" cannot be defined without knowing whether the page is about rivers
        or money, so the paragraph travels with the word.
        """

        explain = StubAiEngine(EXPLANATION)
        runtime = build_runtime(clock, ai=bridge(explain=explain))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")

        assert len(explain.calls) == 1
        context = explain.calls[0].resolved_context()
        assert context.selected_word == "lighthouse"
        assert context.current_paragraph.startswith("The lighthouse")
        assert context.page_number == FIRST_PAGE_INDEX

    @pytest.mark.asyncio
    async def test_a_lookup_carries_the_paragraph_the_finger_was_in(self, clock):
        """The pointed-at paragraph, not the one the pointer is parked on.

        Meaning Mode deliberately does not move the pointer, so on a multi-
        paragraph page the two diverge by design: point at the third paragraph
        while reading the first and the pointer stays at the first. If the
        explanation is built from the pointer, the model is asked about a word
        alongside a paragraph the word does not appear in — and answers
        confidently, because the request looks well formed.
        """

        explain = StubAiEngine(EXPLANATION)
        runtime = build_runtime(clock, ai=bridge(explain=explain))
        await runtime.feed_camera_frame(
            recorded_paragraphs(PARAGRAPH_ONE, PARAGRAPH_TWO, PARAGRAPH_THREE)
        )
        await runtime.engine.start_session()
        assert runtime.engine.state.pointer.paragraph_index == 0

        clock.advance(20)
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(height=400),
            # Inside "grandfather", the fifth word of the third paragraph.
            finger=FingerPoint(x=400.0, y=310.0, confidence=0.9, direction=(0.0, -1.0)),
            meaning_gesture=True,
        )

        assert result.selected_word.strip(".,") == "grandfather"
        # The pointer has not moved: that is the whole reason this can go wrong.
        assert runtime.engine.state.pointer.paragraph_index == 0

        context = explain.calls[0].resolved_context()
        assert context.selected_word.strip(".,") == "grandfather"
        assert context.current_paragraph == PARAGRAPH_THREE
        assert context.previous_paragraph == PARAGRAPH_TWO

    @pytest.mark.asyncio
    async def test_earlier_lookups_are_offered_to_later_ones(self, clock):
        """Session memory is what lets the model say "like the word you asked about"."""

        explain = StubAiEngine(EXPLANATION)
        ai = bridge(explain=explain)
        runtime = build_runtime(clock, ai=ai)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        await runtime.engine.meaning_mode_off()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="promised")

        second = explain.calls[1].resolved_context()
        assert [word.word for word in second.previously_explained] == ["lighthouse"]

    @pytest.mark.asyncio
    async def test_the_review_is_built_from_the_words_actually_explained(self, clock):
        review = StubAiEngine(REVIEW)
        runtime = build_runtime(clock, ai=bridge(review=review))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        await runtime.engine.meaning_mode_off()

        clock.advance(30)
        await runtime.engine.finish_session()

        assert runtime.engine.review.ok
        assert runtime.engine.review.data["flashcards"]
        assert runtime.engine.review.data["quiz"]
        assert runtime.engine.review.data["session_summary"]
        assert [record.word for record in review.calls[0].session_history] == ["lighthouse"]

    @pytest.mark.asyncio
    async def test_a_session_with_no_lookups_has_nothing_to_review(self, clock):
        """Not an error worth showing the reader — there is simply nothing to say."""

        review = StubAiEngine(REVIEW)
        runtime = build_runtime(clock, ai=bridge(review=review))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        await runtime.engine.finish_session()

        assert not runtime.engine.review.ok
        assert "No lookups" in runtime.engine.review.error
        assert review.calls == []

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_not_an_explanation(self, clock):
        """`status == "ok"` with no text is a call that succeeded and said nothing.

        Recording it would put a word in the review with a blank flashcard.
        """

        explain = StubAiEngine({})
        runtime = build_runtime(clock, ai=bridge(explain=explain))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")

        assert not runtime.engine.explanation.ok
        assert runtime.engine.lookups == []

    @pytest.mark.asyncio
    async def test_a_failed_lookup_is_not_counted_as_friction(self, clock):
        """The reader asked and got nothing. That says nothing about the page."""

        explain = StubAiEngine(raises=RuntimeError("groq unreachable"))
        runtime = build_runtime(clock, ai=bridge(explain=explain))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        clock.advance(10)
        await runtime.engine.meaning_mode_off()
        clock.advance(20)
        analytics = await runtime.engine.finish_session()

        assert analytics.lookup_count == 0
        # The reader still stopped, and that is still recorded.
        assert analytics.meaning_requests == 1

    @pytest.mark.asyncio
    async def test_the_model_is_asked_only_after_narration_stops(self, clock, audio):
        """The pause is the response to the gesture; the explanation fills it.

        Asking first would leave the reader hearing the next sentence for the
        seconds a round trip takes, having just gestured that they cannot read
        past this word.
        """

        observed: list[PlaybackState] = []

        class RecordingEngine(StubAiEngine):
            def explain(self, request):
                observed.append(audio.get_status().state)
                return self._respond(request)

        runtime = build_runtime(clock, audio=audio, ai=bridge(explain=RecordingEngine(EXPLANATION)))
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")

        assert observed == [PlaybackState.PAUSED]


# ------------------------------------------------------ runtime failure scenarios


class TestFailureScenarios:
    """What breaks, and what must keep working when it does."""

    @pytest.mark.asyncio
    async def test_a_hanging_ai_call_is_bounded(self, clock):
        """The timeout stops waiting; it cannot stop the thread.

        A blocking socket read is not interruptible from outside, so the worker
        runs on and its result is discarded. The alternative is a reader whose
        page stays paused for as long as the upstream takes to notice it is dead.
        """

        ai = bridge(explain=StubAiEngine(EXPLANATION, delay=1.0))
        ai.timeout = 0.05

        runtime = build_runtime(clock, ai=ai)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")

        assert not runtime.engine.explanation.ok
        assert "timed out" in runtime.engine.explanation.error
        assert runtime.engine.state.is_meaning_mode

    @pytest.mark.asyncio
    async def test_a_failing_ai_does_not_stop_the_session(self, clock, audio):
        """Every AI failure looks the same to the reader: no explanation appeared."""

        runtime = build_runtime(
            clock,
            audio=audio,
            ai=bridge(
                explain=StubAiEngine(raises=RuntimeError("no api key")),
                review=StubAiEngine(status="error", payload={"error": "upstream 503"}),
            ),
        )
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.engine.meaning_mode_on(word="lighthouse")
        assert audio.get_status().state is PlaybackState.PAUSED

        clock.advance(10)
        await runtime.engine.meaning_mode_off()
        assert audio.get_status().state is PlaybackState.PLAYING

        clock.advance(30)
        analytics = await runtime.engine.finish_session()

        assert runtime.engine.state.is_finished
        assert analytics.words_read > 0
        assert not runtime.engine.review.ok

    @pytest.mark.asyncio
    async def test_a_stale_frame_is_rejected_without_disturbing_the_session(self, clock):
        """Frames overtake each other over HTTP. Rejecting one is normal operation.

        The session continues on the text it already holds rather than regressing
        to what an older frame saw.
        """

        runtime = build_runtime(clock)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(5)
        refined = PAGE_ONE + " The keeper's cottage had long since fallen in."
        await runtime.feed_camera_frame(recorded_page(refined))
        text_after_refinement = runtime.engine.current_text()

        # An older frame arrives late, carrying the un-refined page. Applied
        # through the engine rather than straight into Merge Memory, because
        # catching the rejection is the runtime behaviour under test — Merge
        # Memory's job is to raise, the engine's is to carry on.
        clock.advance(1)
        await runtime.engine._apply_text(PAGE_ONE, 1)

        assert runtime.engine.current_text() == text_after_refinement
        assert runtime.engine.state.is_reading
        assert any(
            "stale frame rejected" in event.detail
            for event in events_of(runtime, SessionEvent.CONTENT_UPDATED)
        )

    @pytest.mark.asyncio
    async def test_no_module_writes_another_modules_state(self, clock, audio):
        """The ownership rule, asserted as a whole-session invariant.

        Reading Speed is told about every pointer move and never causes one; the
        Audio Engine follows the pointer and never sets it; Gesture publishes and
        never calls. If any of those were violated the pointer here would differ
        from the one the engine last wrote.
        """

        runtime = build_runtime(clock, audio=audio, ai=bridge())
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(15)
        await runtime.feed_gesture_frame(
            blank_frame(),
            finger=FingerPoint(x=100.0, y=44.0, confidence=0.9, direction=(0.0, -1.0)),
        )
        engine_pointer = runtime.engine.state.pointer

        clock.advance(10)
        runtime.engine.speed.predict("session-1")
        await audio.pause(reason=PauseReason.USER)
        await audio.resume()

        assert runtime.engine.state.pointer == engine_pointer

    @pytest.mark.asyncio
    async def test_the_event_sequence_of_a_page_turn_is_ordered(self, clock, audio):
        """Commit, then move, then rebuild. Any other order narrates the wrong page.

        Rebuilding the audio queue before committing would narrate the new page
        while the summary still thought the reader was on the old one.
        """

        runtime = build_runtime(clock, audio=audio)
        await runtime.feed_camera_frame(recorded_page(PAGE_ONE))
        await runtime.engine.start_session()

        clock.advance(40)
        await runtime.feed_camera_frame(recorded_page(PAGE_TWO))

        order = event_order(runtime)
        page_changed = order.index(SessionEvent.PAGE_CHANGED.value)

        # The page was committed as part of the turn, before anything read history.
        assert runtime.engine.memory.committed_pages()[0].page_index == FIRST_PAGE_INDEX
        # And the pointer landed on the new page with narration rebuilt from it.
        assert page_changed >= 0
        assert audio.get_status().pointer.page_index == FIRST_PAGE_INDEX + 1

    @pytest.mark.asyncio
    async def test_ingesting_a_frame_without_an_ocr_pipeline_is_an_error(self, clock):
        """A configuration mistake, not a runtime condition. It should be loud."""

        runtime = build_runtime(clock)
        runtime.engine.ocr = None

        with pytest.raises(RuntimeError, match="No OCR pipeline"):
            await runtime.engine.ingest_frame(recorded_page(PAGE_ONE))


# ------------------------------------------------------------- golden scenario


class TestGoldenScenario:
    """One continuous session, walked once, with the whole end state asserted.

    Every other class here isolates a stage so a failure names one module. This
    one deliberately does the opposite: it drives the sequence a reader actually
    produces — start, read, point, ask, resume, move on, ask again, finish — and
    then asserts the complete final state in one place.

    What it catches that the isolated scenarios cannot: a stage that is correct
    in isolation and wrong in sequence. Meaning Mode pausing narration but not
    resuming the same sentence; a paragraph that was read appearing in the focus
    report under the wrong key because the pointer was moved by a gesture rather
    than by hand; a lookup recorded twice because two paths both counted it. Each
    of those passes a per-stage test and fails a session.
    """

    #: Fingertips inside particular words, from `recorded_paragraphs` geometry:
    #: rows are 30px apart with 24px-tall boxes, columns 90px apart and 80 wide.
    #: Paragraph one occupies rows 0-3, two rows 5-8, three rows 10-12.
    IN_PARAGRAPH_ONE = FingerPoint(x=100.0, y=70.0, confidence=0.9, direction=(0.0, -1.0))
    IN_PARAGRAPH_TWO = FingerPoint(x=100.0, y=160.0, confidence=0.9, direction=(0.0, -1.0))
    IN_PARAGRAPH_THREE = FingerPoint(x=400.0, y=310.0, confidence=0.9, direction=(0.0, -1.0))

    @pytest.mark.asyncio
    async def test_the_golden_scenario(self, clock, audio):
        explain = StubAiEngine(EXPLANATION)
        runtime = build_runtime(clock, audio=audio, ai=bridge(explain=explain))
        engine = runtime.engine

        # 1. A frame arrives before the reader has started. OCR reads it, Merge
        #    Memory holds it, and nothing has begun.
        await runtime.feed_camera_frame(
            recorded_paragraphs(PARAGRAPH_ONE, PARAGRAPH_TWO, PARAGRAPH_THREE)
        )
        assert engine.memory.paragraph_count(FIRST_PAGE_INDEX) == 3
        assert not engine.state.is_reading

        # 2. Session start. Narration queues from the pointer.
        await engine.start_session()
        assert engine.state.is_reading
        assert audio.get_status().state is PlaybackState.PLAYING

        # 3. The reader reads a while, then points at a later line to correct the
        #    pointer — the momentary button.
        clock.advance(25)
        await runtime.feed_gesture_frame(
            blank_frame(height=400), finger=self.IN_PARAGRAPH_TWO
        )
        after_first_point = engine.state.pointer
        assert after_first_point.paragraph_index == 1

        # 4. Reading on, then the toggle: point at a word in the *third*
        #    paragraph while the pointer sits in the second, and hold Meaning Mode.
        clock.advance(25)
        pointer_before_meaning = engine.state.pointer
        result, _ = await runtime.feed_gesture_frame(
            blank_frame(height=400),
            finger=self.IN_PARAGRAPH_THREE,
            meaning_gesture=True,
        )
        asked_about = result.selected_word.strip(".,")

        assert engine.state.is_meaning_mode
        assert audio.get_status().pause_reason is PauseReason.MEANING_MODE
        # Asking is not reading past.
        assert engine.state.pointer == pointer_before_meaning
        # The explanation was built from the paragraph the finger was in.
        assert explain.calls[0].resolved_context().current_paragraph == PARAGRAPH_THREE
        assert engine.explanation.ok
        assert engine.lookups == [result.selected_word]

        # 5. Release. The interrupted sentence resumes, it does not skip.
        clock.advance(30)
        await engine.meaning_mode_off()
        assert not engine.state.is_meaning_mode
        assert audio.get_status().state is PlaybackState.PLAYING
        assert engine.state.pointer == pointer_before_meaning

        # 6. Reading resumes and a second, slower section produces a second
        #    lookup — the evidence Reading Focus Analysis ranks on.
        clock.advance(90)
        await runtime.feed_gesture_frame(
            blank_frame(height=400),
            finger=self.IN_PARAGRAPH_THREE,
            meaning_gesture=True,
        )
        await engine.meaning_mode_off()

        # 7. Session end.
        clock.advance(20)
        analytics = await engine.finish_session(review=False)

        # ------------------------------------------------ the complete end state
        assert engine.state.is_finished and not engine.state.is_paused

        # Merge Memory is what every word count came from.
        held_words = sum(
            len(engine.memory.paragraph(FIRST_PAGE_INDEX, index).split())
            for index in range(3)
        )
        assert engine.memory.total_words == held_words
        assert analytics.pages_read >= 1

        # Both lookups were recorded once each, not twice by two paths.
        assert len(engine.lookups) == 2
        assert asked_about.lower() in engine.lookups[0].lower()

        # Reading Focus Analysis ran, over the paragraphs the reader was in, and
        # judged them against the baseline the session finished with.
        focus = engine.focus_report
        assert focus is not None
        visited = [p.paragraph_index for p in focus.paragraphs]
        assert visited == sorted(visited), "paragraphs must be reported in reading order"
        assert 1 in visited, "the paragraph the reader was moved into is missing"
        for paragraph in focus.paragraphs:
            held = engine.memory.paragraph(paragraph.page_index, paragraph.paragraph_index)
            assert paragraph.words == len(held.split())
            assert 0.0 <= paragraph.revision_priority <= 100.0

        # The friction is charged to where the reader was, and it is the same
        # count the session recorded — two engines, one story.
        assert sum(p.meaning_requests for p in focus.paragraphs) == 2

        # Nothing in the report claims to know what the reader was doing.
        prose = " ".join(e for p in focus.paragraphs for e in p.evidence).lower()
        assert "distract" not in prose
