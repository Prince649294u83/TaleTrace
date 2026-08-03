"""The assembled runtime: every module wired into one chain, once.

    ESP32 frame
        |
        v
    OcrPipeline ──> MergeMemory ──> ReadingEngine ──> ReadingSpeedService
        |                               |    |
        |                               |    +──> PlaybackEngine (narration)
        +──> GesturePipeline ─events─┘    +──> AiBridge (meaning, review)

`ReadingEngine` sequences the modules; this builds them and connects the two
edges that cannot be expressed as a direct call.

Edge one: Gesture publishes synchronously, the Reading Engine handles
asynchronously. `GesturePipeline.publish` is a plain callable — it has to be,
because it is invoked from inside frame processing that may run in a worker
thread and must not be blocked on a network round trip to a TTS provider. So
published events land in a queue here and are drained into the engine's async
handlers. The gesture pipeline still never calls another module; it hands an
event to a callback and returns.

Edge two: Gesture reports lines, the pointer counts sentences. A line of OCR text
is not a sentence — a sentence spans two or three lines, and one line can end a
sentence and begin another. Translating between them needs the paragraph text,
which Gesture does not have and should not be given. `_sentence_for_line` does it
here, against Merge Memory's paragraph and the Audio Engine's own segmenter, so
the pointer Gesture causes and the sentence Audio speaks agree by construction.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.audio_engine.sentence_queue import segment_sentences
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
        memory: MergeMemory | None = None,
        speed: ReadingSpeedService | None = None,
    ) -> ReadingRuntime:
        """Assemble a session from parts, defaulting each to its real implementation.

        Every collaborator is overridable because the failure scenarios need it:
        an OCR provider that raises, an audio engine that never finishes a
        sentence, an AI engine that times out. Left alone, this builds the same
        chain the live runtime uses.

        `memory` and `speed` are accepted because they may already exist — a
        `ReadingSpeedService` holds one reader's calibrated baseline across
        sessions, and rebuilding it per session would discard the calibration.
        """

        shared_memory = memory if memory is not None else MergeMemory(reconstruct=reconstruct)
        shared_speed = (
            speed if speed is not None
            else (ReadingSpeedService(clock=clock) if clock else ReadingSpeedService())
        )

        pipeline = OcrPipeline(provider=ocr_provider) if ocr_provider else OcrPipeline()

        engine = ReadingEngine(
            session_id=session_id,
            reader_id=reader_id,
            memory=shared_memory,
            speed=shared_speed,
            ocr=pipeline,
            audio=audio,
            ai=ai,
            book_id=book_id,
            **({"clock": clock} if clock else {}),
        )

        gesture = GesturePipeline(**({"clock": clock} if clock else {}))

        return cls(engine=engine, gesture=gesture)

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
    ) -> SelectionResult:
        """A frame from the device, for pointing. Publishes, then drains.

        The words come from the OCR pipeline's current page rather than from a
        separate recognition pass — the reader points at the page the system has
        already read, and running OCR twice would let the two disagree about where
        a word is.
        """

        words = self.engine.ocr.words if self.engine.ocr is not None else ()
        result = self.gesture.observe(
            image, words, finger=finger, meaning_gesture=meaning_gesture
        )
        self.last_selection = result
        await self.drain()
        return result

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

    async def drain(self) -> int:
        """Hand every queued gesture event to the engine, in order.

        Returns how many were handled. Order is preserved because these events
        are not independent: a `WORD_SELECTED` followed by a `MEANING_REQUESTED`
        is the reader pointing and then asking, and handling them out of order
        would look up whatever word came next.
        """

        handled = 0
        while self._pending:
            event, payload = self._pending.popleft()
            await self.engine.handle_gesture_event(event, self._translate(event, payload))
            handled += 1
        return handled

    def _translate(
        self, event: SessionEvent, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Fill in what Gesture cannot know: which sentence a line belongs to."""

        if event is not SessionEvent.READING_POINTER_UPDATED:
            return payload

        sentence = self._sentence_for_line(str(payload.get("line_text", "")))
        if sentence is None:
            # No match: the line came from text Merge Memory does not hold yet,
            # which happens when a gesture arrives before the frame that read
            # that part of the page. Dropping the sentence index leaves the
            # engine on the pointer it has, which is better than guessing.
            return payload

        return {**payload, "sentence_index": sentence}

    def _sentence_for_line(self, line_text: str) -> int | None:
        """Which sentence of the current paragraph a line of OCR text falls in.

        Matched by word overlap rather than substring: the line comes from OCR
        and the paragraph from Merge Memory's merge of several frames, so the two
        rarely agree character for character even when they describe the same
        words. The same reason `detect_new_page` compares word sets.
        """

        words = {w.lower().strip(".,;:!?\"'") for w in line_text.split()}
        words.discard("")
        if not words:
            return None

        paragraph = self.engine.current_text()
        if not paragraph.strip():
            return None

        best_index: int | None = None
        best_overlap = 0

        for chunk in segment_sentences(paragraph):
            candidate = {w.lower().strip(".,;:!?\"'") for w in chunk.text.split()}
            overlap = len(words & candidate)
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = chunk.pointer.sentence_index

        # A single shared word is coincidence ("the"), not a match. Two is the
        # cheapest threshold that rejects it without needing the line to be
        # mostly present — an OCR line is often three or four words long.
        if best_overlap < 2:
            return None
        return best_index
