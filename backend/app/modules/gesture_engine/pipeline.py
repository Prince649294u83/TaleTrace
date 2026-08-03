"""The one gesture pipeline: a frame becomes events, never a call into another module.

Replaces `OCRandGESTURE/Gesture/gesture_engine/__init__.select_word`. That
function returned a `SelectionResult` and left the caller to decide what to do
with it; the standalone controller then reached into the audio player and the
lookup path directly. This module keeps the same detect-then-select core and adds
the one thing the architecture requires: the outcome leaves as events.

    process_frame()  frame + current page words -> SelectionResult
    observe()        the same, then publishes what happened

Why events and not calls
------------------------
Gesture cannot know what pointing at a word should do. Mid-sentence it is a
lookup; on the last word of a page it may be a page turn; in Meaning Mode it is
already a lookup and a second one is a no-op. Those decisions need session state
that Gesture does not own and must not read around. Publishing
`WORD_SELECTED` and letting the Reading Engine decide is what keeps the pointer
single-writer.

The four events, and when each fires:

    READING_POINTER_UPDATED   the reader has moved to a new line — they are
                              reading, not asking
    WORD_SELECTED             a specific word was resolved with confidence
    MEANING_REQUESTED         the selection was a meaning gesture
    CAMERA_OFF                no frame arrived; the camera is gone

A low-confidence selection publishes nothing. The reader gets no wrong answer
rather than a plausible one — `LOW_CONFIDENCE` exists precisely so the runtime can
tell "they pointed at something I could not resolve" from "they did not point".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.app.modules.gesture_engine.detector import detect_finger
from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
)
from backend.app.modules.gesture_engine.selector import select_intended_word
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.shared.events import SessionEvent

API_VERSION = "2.0"

# How far the fingertip must move down the page, in multiples of a line height,
# before it counts as the reader advancing rather than hovering. A resting hand
# drifts a few pixels a frame; without this every jitter would publish a pointer
# update and Reading Speed would see a reader moving at an impossible pace.
LINE_ADVANCE_RATIO = 0.8


@dataclass
class GesturePipeline:
    """Frames in, events out. Holds only what it needs to detect a change.

    One instance per session. The `publish` callback is injected rather than
    imported so the pipeline can be driven by a test, a demo, or the live runtime
    without knowing which — and so this module has no import edge to the event
    bus, the Reading Engine, or the Audio Engine.
    """

    publish: Callable[[SessionEvent, dict[str, Any]], None] | None = None
    config: SelectionConfig = field(default_factory=SelectionConfig)
    # Injected for the same reason the Audio Engine's is: a scenario replays
    # frames faster than real time, and a wall clock would report a reader moving
    # at several thousand words a minute.
    clock: Callable[[], float] = time.perf_counter

    _last_line_y: float | None = field(default=None, init=False)
    _last_word: str = field(default="", init=False)

    def process_frame(
        self,
        image: Any,
        words: list[RecognizedWord] | tuple[RecognizedWord, ...],
        *,
        finger: FingerPoint | None = None,
    ) -> SelectionResult:
        """Resolve what the reader is pointing at. Publishes nothing.

        Separated from `observe` so the selection can be exercised without an
        event sink — and so the runtime can inspect a result before deciding it
        is worth acting on. `finger` may be supplied directly to bypass detection,
        which is how a recorded scenario replays a gesture without a camera.
        """

        started = self.clock()

        if image is None or getattr(image, "size", 0) == 0:
            return SelectionResult(
                status=SelectionStatus.NO_FINGER,
                selection_time_ms=(self.clock() - started) * 1000.0,
            )

        height, width = image.shape[:2]
        image_size = (width, height)

        if not words:
            return SelectionResult(
                status=SelectionStatus.OCR_EMPTY,
                image_size=image_size,
                selection_time_ms=(self.clock() - started) * 1000.0,
            )

        point = finger if finger is not None else detect_finger(image, self.config)
        if point is None:
            return SelectionResult(
                status=SelectionStatus.NO_FINGER,
                image_size=image_size,
                selection_time_ms=(self.clock() - started) * 1000.0,
            )

        result = select_intended_word(point, list(words), self.config)
        return result.model_copy(
            update={
                "image_size": image_size,
                "selection_time_ms": (self.clock() - started) * 1000.0,
            }
        )

    def observe(
        self,
        image: Any,
        words: list[RecognizedWord] | tuple[RecognizedWord, ...],
        *,
        finger: FingerPoint | None = None,
        meaning_gesture: bool = False,
    ) -> SelectionResult:
        """Process a frame and publish what it turned out to mean.

        `meaning_gesture` is passed in rather than inferred here. Whether a hand
        shape counts as "explain this" depends on the reader's configured gesture
        set, which lives in session settings — inferring it from the fingertip
        alone would make the pipeline guess at a preference it cannot see.
        """

        result = self.process_frame(image, words, finger=finger)

        if result.status is SelectionStatus.NO_FINGER and image is None:
            self._emit(SessionEvent.CAMERA_OFF, {"reason": "no frame delivered"})
            return result

        if not result.succeeded:
            # LOW_CONFIDENCE, NO_WORD_FOUND, OCR_EMPTY and NO_FINGER all end here.
            # None of them are worth waking another module for, and publishing a
            # guess is worse than publishing nothing.
            return result

        if meaning_gesture:
            self._emit(
                SessionEvent.MEANING_REQUESTED,
                {
                    "word": result.selected_word,
                    "context": result.context,
                    "confidence": result.confidence,
                    "page_word_index": result.word_index,
                },
            )
            self._last_word = result.selected_word
            return result

        if self._advanced_a_line(result):
            self._emit(
                SessionEvent.READING_POINTER_UPDATED,
                {
                    "line_index": result.line_index,
                    "paragraph_index": result.paragraph_index,
                    "word_index": result.word_index,
                    "line_text": result.selected_line,
                    "confidence": result.confidence,
                },
            )

        if result.selected_word != self._last_word:
            self._emit(
                SessionEvent.WORD_SELECTED,
                {
                    "word": result.selected_word,
                    "context": result.context,
                    "line_text": result.selected_line,
                    "confidence": result.confidence,
                    "bbox": result.selected_word_bbox,
                },
            )
            self._last_word = result.selected_word

        return result

    def reset(self) -> None:
        """Forget the last position. Called on a page turn.

        Without this the first selection on a new page compares against a line
        position from the previous one, and a reader who turns back to the top of
        a page reads as having jumped backwards.
        """

        self._last_line_y = None
        self._last_word = ""

    def _advanced_a_line(self, result: SelectionResult) -> bool:
        """Whether this selection sits far enough below the last to be progress."""

        if result.finger_point is None:
            return False

        current_y = result.finger_point.y
        previous_y = self._last_line_y
        self._last_line_y = current_y

        if previous_y is None:
            return True

        height = 0.0
        if result.selected_word_bbox is not None:
            height = float(result.selected_word_bbox[3] - result.selected_word_bbox[1])
        threshold = max(1.0, height * LINE_ADVANCE_RATIO)
        return abs(current_y - previous_y) >= threshold

    def _emit(self, event: SessionEvent, payload: dict[str, Any]) -> None:
        if self.publish is not None:
            self.publish(event, payload)


def select_word(
    image: Any,
    ocr_words: list[RecognizedWord] | tuple[RecognizedWord, ...] | None = None,
    config: SelectionConfig | None = None,
) -> SelectionResult:
    """Stateless one-shot selection, kept for callers that only want the answer.

    The standalone entry point had this signature and the demos still call it.
    It builds a throwaway pipeline with no publisher, so it cannot emit events —
    a caller that wants those needs a `GesturePipeline` it holds onto, because
    detecting a *change* requires remembering the previous frame.
    """

    pipeline = GesturePipeline(config=config or SelectionConfig())
    return pipeline.process_frame(image, list(ocr_words or []))
