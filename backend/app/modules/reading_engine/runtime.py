"""The assembled runtime: every module wired into one chain, once.

    ESP32 frame
        |
        v
    OcrPipeline ──> MergeMemory ──> ReadingEngine ──> ReadingSpeedService
        |                               |    |
        |                               |    +──> PlaybackEngine (narration)
        +──> GesturePipeline ─events─┘    +──> AiBridge (meaning, review)
                                        |
                                        +──> FocusAnalyticsEngine (observes only)

`ReadingEngine` sequences the modules; this builds them and connects the two
edges that cannot be expressed as a direct call.

Edge one: Gesture publishes synchronously, the Reading Engine handles
asynchronously. `GesturePipeline.publish` is a plain callable — it has to be,
because it is invoked from inside frame processing that may run in a worker
thread and must not be blocked on a network round trip to a TTS provider. So
published events land in a queue here and are drained into the engine's async
handlers. The gesture pipeline still never calls another module; it hands an
event to a callback and returns.

Edge two: Gesture counts paragraphs and lines the way OCR saw them, the pointer
counts paragraphs and sentences the way Merge Memory holds them, and those are
two different coordinate systems. A line of OCR text is not a sentence — a
sentence spans two or three lines, and one line can end a sentence and begin
another. Nor is an OCR paragraph a Merge Memory paragraph: Vision reports a
paragraph per visual block, so a photographed page comes back as dozens of
fragments, and reconstruction rejoins them into the handful the page actually
has. A gesture reporting "paragraph 20" of 47 means nothing to a memory holding 4.

Translating both needs the page text, which Gesture does not have and should not
be given. `_locate_line` does it here, against Merge Memory's paragraphs and the
Audio Engine's own segmenter, so the pointer Gesture causes and the sentence Audio
speaks agree by construction.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.focus_analytics import FocusAnalyticsEngine
from backend.app.modules.gesture_engine.pipeline import GesturePipeline
from backend.app.modules.gesture_engine.selection_models import FingerPoint, SelectionResult
from backend.app.modules.merge_memory.engine import MergeMemory
from backend.app.modules.ocr.pipeline import FrameResult, OcrPipeline
from backend.app.modules.reading_engine.engine import ReadingEngine
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared.events import SessionEvent

logger = logging.getLogger(__name__)


@dataclass
class ReadingRuntime:
    """One reading session, fully wired. The object a route or a demo holds.

    Nothing here duplicates a module's work: it constructs them, owns the queue
    between Gesture and the engine, and translates a line index into a sentence
    index. Every other decision belongs to whichever module owns it.
    """

    engine: ReadingEngine
    gesture: GesturePipeline
    _pending: deque[tuple[SessionEvent, dict[str, Any]]] = field(
        default_factory=deque, init=False
    )
    # The last outcome of each camera path, kept for the monitor. Not state any
    # module owns — it is the answer to "is OCR working right now", which is
    # otherwise unanswerable: a pipeline that returned nothing looks identical to
    # one that was never called, and those need different responses from a user.
    last_frame: FrameResult | None = field(default=None, init=False)
    last_selection: SelectionResult | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Wired after construction rather than passed in, because the callback
        # closes over this instance and cannot exist before it does.
        self.gesture.publish = self._enqueue

    # ------------------------------------------------------------------ building

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        reader_id: str,
        ocr_provider: Any = None,
        audio: Any = None,
        ai: Any = None,
        book_id: str | None = None,
        clock: Callable[[], float] | None = None,
        reconstruct: Callable[[str, str], str] | None = None,
        confirm_page_change: Callable[[str, str], bool] | None = None,
        memory: MergeMemory | None = None,
        speed: ReadingSpeedService | None = None,
        focus: bool = True,
    ) -> ReadingRuntime:
        """Assemble a session from parts, defaulting each to its real implementation.

        Every collaborator is overridable because the failure scenarios need it:
        an OCR provider that raises, an audio engine that never finishes a
        sentence, an AI engine that times out. Left alone, this builds the same
        chain the live runtime uses.

        `memory` and `speed` are accepted because they may already exist — a
        `ReadingSpeedService` holds one reader's calibrated baseline across
        sessions, and rebuilding it per session would discard the calibration.

        `reconstruct` and `confirm_page_change` are the two halves of the
        Groq-backed Merge Engine, passed in rather than constructed here so a
        session can run without a key and a test can run without a network. See
        `build_live` for the wiring that supplies both.

        `focus` is a flag rather than an instance, unlike every other
        collaborator here. There is nothing to substitute: the Reading Focus
        Analysis engine has no network, no provider and no failure mode worth
        faking, and it needs the same `memory` and `clock` this method has just
        settled on. Passing one in would mean a caller could hand it a *different*
        Merge Memory than the session's, and the word counts in the report would
        then describe a book nobody read.
        """

        shared_memory = memory if memory is not None else MergeMemory(reconstruct=reconstruct)
        shared_speed = (
            speed if speed is not None
            else (ReadingSpeedService(clock=clock) if clock else ReadingSpeedService())
        )

        pipeline = OcrPipeline(provider=ocr_provider) if ocr_provider else OcrPipeline()
        pipeline.confirm_page_change = confirm_page_change

        focus_engine = (
            FocusAnalyticsEngine(
                session_id=session_id,
                reader_id=reader_id,
                memory=shared_memory,
                baseline=shared_speed.baseline_for(reader_id),
                **({"clock": clock} if clock else {}),
            )
            if focus
            else None
        )

        engine = ReadingEngine(
            session_id=session_id,
            reader_id=reader_id,
            memory=shared_memory,
            speed=shared_speed,
            ocr=pipeline,
            audio=audio,
            ai=ai,
            focus=focus_engine,
            book_id=book_id,
            **({"clock": clock} if clock else {}),
        )

        gesture = GesturePipeline(**({"clock": clock} if clock else {}))

        return cls(engine=engine, gesture=gesture)

    @classmethod
    def build_live(
        cls,
        *,
        session_id: str,
        reader_id: str,
        book_id: str | None = None,
        audio: Any = None,
        ai: Any = None,
        speed: ReadingSpeedService | None = None,
    ) -> ReadingRuntime:
        """The production wiring: Google Vision, Groq reconstruction, real Gesture.

        The one place the live chain is assembled, so there is no second way to
        start a session. `build` stays the general form that tests and demos use;
        this is `build` with the production collaborators filled in.

        Both halves of the Merge Engine come from one `GroqReconstructor`: the
        same client answers "merge these two texts" and "is this the same page",
        and giving them separate instances would open two clients per session.
        That client is the Merge Engine's own, built from `GROQ_API_KEY_2`. The AI
        Engine builds its own from `GROQ_API_KEY_1` and is passed in as `ai` — the
        two subsystems share no client, so a rate limit on the camera loop cannot
        stop a reader from asking what a word means.
        """

        from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
        from backend.app.modules.ocr.providers import get_ocr_engine

        reconstructor = GroqReconstructor()
        if not reconstructor.available:
            # Said once, at startup, naming the consequence rather than the
            # variable: text will still flow, it will just be rougher.
            logger.warning(
                "GROQ_API_KEY_2 is not set — page text will be raw OCR with no "
                "semantic reconstruction, and page turns will use geometry alone"
            )

        return cls.build(
            session_id=session_id,
            reader_id=reader_id,
            book_id=book_id,
            ocr_provider=get_ocr_engine(),
            audio=audio,
            ai=ai,
            speed=speed,
            reconstruct=reconstructor,
            confirm_page_change=reconstructor.is_same_page,
        )

    # -------------------------------------------------------------------- frames

    async def feed_camera_frame(self, source: Any) -> FrameResult:
        """A frame from the device, for text. OCR's half of the camera.

        Kept apart from `feed_gesture_frame` because the two consume the same
        frame for unrelated purposes and fail independently: OCR can find no text
        on a frame where the fingertip is perfectly clear, and vice versa.
        """

        result = await self.engine.ingest_frame(source)
        self.last_frame = result

        if result.page_changed:
            # The engine resets the OCR pipeline; Gesture is reset here because
            # the engine holds no reference to it. Without this the first
            # selection on the new page is compared against a line position from
            # the old one, and a reader who turns to the top of a page reads as
            # having jumped backwards.
            self.gesture.reset()

        return result

    async def feed_gesture_frame(
        self,
        image: Any,
        *,
        finger: FingerPoint | None = None,
        meaning_gesture: bool = False,
    ) -> tuple[SelectionResult, list[Any]]:
        """A frame from the device, for pointing. Publishes, then drains.

        The words come from the OCR pipeline's current page rather than from a
        separate recognition pass — the reader points at the page the system has
        already read, and running OCR twice would let the two disagree about where
        a word is.

        Returns ``(selection, results)`` where ``results`` is the list of values
        returned by the event handlers (typically ``MeaningLookupResult`` or
        ``None``).  DeviceLoop uses this to send lookups to the OLED.
        """

        words = self.engine.ocr.words if self.engine.ocr is not None else ()
        result = self.gesture.observe(
            image, words, finger=finger, meaning_gesture=meaning_gesture
        )
        self.last_selection = result
        drain_results = await self.drain()
        return result, drain_results

    # -------------------------------------------------------------------- events

    def _enqueue(self, event: SessionEvent, payload: dict[str, Any]) -> None:
        """Gesture's publish target. Records, never acts.

        Synchronous and cheap on purpose: this may be called from a frame-
        processing thread, and anything slower than an append here shows up as
        dropped frames.
        """

        self._pending.append((event, payload))

    @property
    def pending(self) -> list[tuple[SessionEvent, dict[str, Any]]]:
        """Events published but not yet handled. Read by the dashboard."""

        return list(self._pending)

    async def drain(self) -> list[Any]:
        """Hand every queued gesture event to the engine, in order.

        Returns a list of values from each handler.  Most events return ``None``;
        ``MEANING_REQUESTED`` returns a ``MeaningLookupResult`` when the AI
        produced an explanation, giving the caller what it needs for the OLED
        without reaching into engine state.

        Order is preserved because these events are not independent: a
        ``WORD_SELECTED`` followed by a ``MEANING_REQUESTED`` is the reader
        pointing and then asking, and handling them out of order would look up
        whatever word came next.
        """

        results: list[Any] = []
        while self._pending:
            event, payload = self._pending.popleft()
            result = await self.engine.handle_gesture_event(event, self._translate(event, payload))
            results.append(result)
        return results

    def _translate(
        self, event: SessionEvent, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Fill in what Gesture cannot know: where its line sits in Merge Memory."""

        if event is SessionEvent.MEANING_REQUESTED:
            # The sentence rather than the line, because Gesture publishes the
            # containing sentence here and it is the better match key: more words
            # to overlap on than a line, and already the unit Merge Memory
            # segments into.
            #
            # This matters more than for a pointer update. Meaning Mode does not
            # move the pointer, so on a multi-paragraph page the pointed-at
            # paragraph and the pointer's paragraph diverge *by design* — and an
            # explanation built from the pointer asks the model about a word next
            # to a paragraph the word does not appear in. The request looks well
            # formed, so the answer comes back confident and wrong.
            located = self._locate_line(str(payload.get("context", "")))
            if located is None:
                return payload
            return {**payload, "paragraph_index": located[0]}

        if event is not SessionEvent.READING_POINTER_UPDATED:
            return payload

        located = self._locate_line(str(payload.get("line_text", "")))
        if located is None:
            # No match: the line came from text Merge Memory does not hold yet,
            # which happens when a gesture arrives before the frame that read
            # that part of the page. Dropping both indices leaves the engine on
            # the pointer it has, which is better than guessing — and better than
            # forwarding Gesture's raw OCR paragraph index, which is a position in
            # a different coordinate system and would move the pointer off the page.
            return {k: v for k, v in payload.items() if k != "paragraph_index"}

        paragraph, sentence = located
        return {**payload, "paragraph_index": paragraph, "sentence_index": sentence}

    def _locate_line(self, line_text: str) -> tuple[int, int] | None:
        """Where a line of OCR text falls in Merge Memory: (paragraph, sentence).

        Matched by word overlap rather than substring: the line comes from OCR
        and the paragraphs from Merge Memory's reconstruction of several frames,
        so the two rarely agree character for character even when they describe
        the same words. The same reason `detect_new_page` compares word sets.

        The whole page is searched, not just the paragraph the pointer is in,
        because finding the paragraph is half the question being asked. A reader
        pointing at a word four paragraphs down has moved paragraph as well as
        sentence, and only the text can say which one they landed in.
        """

        words = {w.lower().strip(".,;:!?\"'") for w in line_text.split()}
        words.discard("")
        if not words:
            return None

        page = self.engine.state.pointer.page_index
        best: tuple[int, int] | None = None
        best_overlap = 0

        for paragraph_index in range(self.engine.memory.paragraph_count(page)):
            paragraph = self.engine.memory.paragraph(page, paragraph_index)
            if not paragraph.strip():
                continue
            for chunk in segment_sentences(paragraph):
                candidate = {w.lower().strip(".,;:!?\"'") for w in chunk.text.split()}
                overlap = len(words & candidate)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best = (paragraph_index, chunk.pointer.sentence_index)

        # A single shared word is coincidence ("the"), not a match. Two is the
        # cheapest threshold that rejects it without needing the line to be
        # mostly present — an OCR line is often three or four words long.
        if best_overlap < 2:
            return None
        return best
