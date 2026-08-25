"""The Reading Engine: the one writer of session state.

Everything in the runtime chain passes through here, and that is the point. OCR
produces text, Gesture produces intent, Reading Speed produces predictions, the
Audio Engine produces speech — but none of them may change where the reader is.
They report; this decides.

    ESP32 -> Image Receiver -> OCR -> Merge Memory -> [Reading Engine] -> Reading Speed
                                                            |                  |
                                                            +-> Audio Engine   |
                                                            +-> AI Engine <----+
                                                            +-> Dashboard

Why this class exists
---------------------
`ReadingEngineInterface` has been a contract with no implementation, and the
demo filled the gap with `SimulatedReadingEngine` over a `FakeMergeMemory`. That
proved the ownership rules but could not prove the integration: the fake never
rejected a stale frame, never saw a provider fail, and never had two consumers
disagree about a sentence index. This is the same shape driven by the real
Merge Memory, the real OCR pipeline and the real Gesture pipeline.

The ordering rule
-----------------
Every event handler does the same three things in the same order:

    1. update the state this class owns
    2. tell Reading Speed
    3. tell the Audio Engine

Reading Speed is always told first because it only records. A failure there
cannot leave audio half-started; the reverse is not true — an audio failure
after Reading Speed has recorded leaves a consistent measurement of a session
whose narration broke, which is the honest outcome.

What this class does not do
---------------------------
It does not decide what a page turn means to OCR, does not score gestures, and
does not call Groq. Those live in their modules. This only sequences them, which
is why it can be read top to bottom to see the whole runtime.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.audio_engine.models import (
    PauseReason,
    PlaybackStatistics,
    ReadingPointer,
)
from backend.app.modules.merge_memory import MergeMemory
from backend.app.modules.ocr.pipeline import FrameResult, OcrPipeline
from backend.app.modules.reading_speed.models import ContentMap
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared.constants import FIRST_PAGE_INDEX
from backend.app.shared.events import SessionEvent
from backend.app.shared.exceptions import StaleContentError
from backend.app.shared.session_state import ReadingSessionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeEvent:
    """One thing that happened, with when it happened and who said so.

    Recorded for the dashboard and for the E2E scenarios, which assert on the
    *order* of events as much as their contents — "the page was committed before
    the queue was rebuilt" is the kind of claim only a sequence can support.
    """

    event: SessionEvent
    detail: str = ""
    at: float = 0.0
    source: str = "reading_engine"


@dataclass
class ReadingEngine:
    """Owns the session. Sequences the modules. Writes the pointer.

    Audio and AI are optional because a session is still a real session without
    them: a reader with narration off and no API key reads a book, and the
    runtime must not require either to run. Reading Speed and Merge Memory are
    not optional — without them there is no measurement and no text.
    """

    session_id: str
    reader_id: str
    memory: MergeMemory
    speed: ReadingSpeedService
    ocr: OcrPipeline | None = None
    audio: Any = None
    # `AiBridge`, typed loosely for the same reason `audio` is: this class holds
    # no import of Groq, and a scenario substitutes a stub that fails on purpose.
    ai: Any = None
    # `FocusAnalyticsEngine`, and optional in the strongest sense: a session with
    # no focus engine is a complete session that produces no focus report. It is
    # notified beside Reading Speed at every call site below and is never asked
    # anything, so nothing this class does can depend on its answers — which is
    # what makes "observes only" a property of the wiring rather than a promise.
    focus: Any = None
    book_id: str | None = None
    # Injected for the same reason every other clock in this system is: a
    # scenario replays an hour of reading in a second, and a wall clock would
    # report the reader moving at several thousand words a minute.
    clock: Callable[[], float] = time.monotonic

    _state: ReadingSessionState = field(init=False)
    _content: ContentMap = field(init=False)
    _events: list[RuntimeEvent] = field(default_factory=list, init=False)
    _last_selected_word: str = field(default="", init=False)
    _lookups: list[str] = field(default_factory=list, init=False)
    # The last explanation and the end-of-session review, held for the dashboard
    # and the frontend to read. Kept rather than returned because a lookup is
    # triggered by a gesture, and a gesture has nobody to return a value to.
    _explanation: Any = field(default=None, init=False)
    _review: Any = field(default=None, init=False)
    # The Reading Focus Analysis of the finished session. Held rather than
    # returned for the same reason as the review, and separate from
    # `SessionAnalytics` because the two answer different questions: the
    # analytics say how fast the reader read, this says which paragraphs cost
    # them the most.
    _focus_report: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._content = self.memory.content_map(source_version=self.memory.version or 1)
        self._state = ReadingSessionState(
            session_id=self.session_id,
            reader_id=self.reader_id,
            book_id=self.book_id,
            pointer=ReadingPointer(
                page_index=FIRST_PAGE_INDEX, paragraph_index=0, sentence_index=0
            ),
            content_version=self.memory.version,
            is_reading=False,
        )

    # ------------------------------------------------------------------ reads

    @property
    def state(self) -> ReadingSessionState:
        """The session state. Read by everyone, written only in here."""

        return self._state

    @property
    def content(self) -> ContentMap:
        return self._content

    @property
    def events(self) -> list[RuntimeEvent]:
        return list(self._events)

    @property
    def lookups(self) -> list[str]:
        """Words the reader asked about. What the AI Engine builds flashcards from."""

        return list(self._lookups)

    @property
    def explanation(self) -> Any:
        """The last word explanation, or `None`. An `AiOutcome` when set."""

        return self._explanation

    @property
    def review(self) -> Any:
        """The end-of-session flashcards, quiz and summary. Set by `finish_session`."""

        return self._review

    @property
    def focus_report(self) -> Any:
        """The Reading Focus Analysis, or `None`. A `FocusReport` once finished."""

        return self._focus_report

    def current_text(self, *, paragraph_index: int | None = None) -> str:
        """Merge Memory's best text for the paragraph being read.

        `paragraph_index` overrides the pointer for a caller that knows the reader
        was somewhere else. Meaning Mode is that caller: pointing at a word
        deliberately does not move the pointer, so the paragraph the finger was in
        is not the paragraph the pointer holds.
        """

        pointer = self._state.pointer
        return self.memory.paragraph(
            pointer.page_index,
            pointer.paragraph_index if paragraph_index is None else paragraph_index,
        )

    def _previous_text(self, *, paragraph_index: int | None = None) -> str:
        """The paragraph before the pointer, for explanation context.

        Empty at the top of a page rather than reaching back to the previous
        page's last paragraph: pages turn on physical sheets, and the preceding
        paragraph across a turn is often a different column or a caption.
        """

        paragraph = (
            self._state.pointer.paragraph_index
            if paragraph_index is None
            else paragraph_index
        )
        if paragraph <= 0:
            return ""
        return self.memory.paragraph(self._state.pointer.page_index, paragraph - 1)

    # --------------------------------------------------------- session events

    async def start_session(self, *, profile: Any = None, voice_id: str | None = None) -> None:
        """SESSION_STARTED. Both consumers begin from the same pointer."""

        self._state = self._state.model_copy(
            update={
                "is_reading": True,
                "is_paused": False,
                "is_meaning_mode": False,
                "is_finished": False,
                "tts_enabled": self.audio is not None,
            }
        )
        self._record(SessionEvent.SESSION_STARTED, f"page {self._state.pointer.page_index}")

        self.speed.start_session(
            session_id=self.session_id,
            reader_id=self.reader_id,
            pointer=self._state.pointer,
            content=self._content,
        )
        if self.focus is not None:
            self.focus.session_started(self._state.pointer)

        if self.audio is not None:
            await self.audio.start(
                pointer=self._state.pointer,
                text=self.current_text(),
                profile=profile,
                voice_id=voice_id,
            )

    async def pause(self) -> None:
        """SESSION_PAUSED. The reader set the book down — says nothing about the text."""

        self._state = self._state.model_copy(update={"is_paused": True})
        self._record(SessionEvent.SESSION_PAUSED, "")
        self.speed.pause(self.session_id)
        if self.focus is not None:
            self.focus.session_paused()
        if self.audio is not None:
            await self.audio.pause(reason=PauseReason.USER)

    async def resume(self) -> None:
        """SESSION_RESUMED."""

        self._state = self._state.model_copy(
            update={"is_paused": False, "is_meaning_mode": False}
        )
        self._record(SessionEvent.SESSION_RESUMED, "")
        self.speed.resume(self.session_id)
        if self.focus is not None:
            self.focus.session_resumed()
        if self.audio is not None:
            await self.audio.resume()

    async def finish_session(self, *, ingest_tts: bool = True, review: bool = True):
        """SESSION_FINISHED. Stops both clocks, commits the page, returns the analysis.

        The open page is committed before the summary is built. A page left
        uncommitted is invisible to `committed_pages()`, so the summary would
        describe every page the reader read except the one they just finished.

        The AI review is built last and its result is kept on the engine rather
        than returned, so the return type stays `SessionAnalytics` whether or not
        an AI Engine is attached. `review=False` skips the model call for a
        scenario that only cares about the measurements.
        """

        self.memory.commit_page()
        self._state = self._state.model_copy(
            update={
                "is_reading": False,
                "is_paused": False,
                "is_meaning_mode": False,
                "is_finished": True,
            }
        )
        self._record(SessionEvent.SESSION_FINISHED, "")

        playback: PlaybackStatistics | None = None
        if self.audio is not None:
            await self.audio.stop()
            if ingest_tts:
                # The engine counted words while speaking, which beats inferring
                # them from pointer movement.
                playback = self.audio.final_statistics

        analytics = self.speed.finish_session(self.session_id, playback=playback)

        focus_report = None
        if self.focus is not None:
            self.focus.session_finished()
            # Judged against the baseline as it stands *now*, not the one the
            # focus engine was built with. A session that calibrated the reader
            # part-way through knows more at the end than it did at the start,
            # and `report()` taking an override is exactly so the paragraphs can
            # be re-judged without replaying the events.
            focus_report = self.focus.report(self.speed.baseline_for(self.reader_id))
            self._focus_report = focus_report
            self._record(
                SessionEvent.SESSION_FINISHED,
                f"focus analysis: {len(focus_report.paragraphs)} paragraph(s), "
                f"{len(focus_report.needs_attention())} needing attention",
            )

        if self.ai is not None and review:
            # After the analytics, not before: the review is the slowest thing
            # this method does, and a model that hangs must not be the reason a
            # finished session has no measurements.
            self._review = await self.ai.review(pages_read=analytics.pages_read)
            self._record(
                SessionEvent.SESSION_FINISHED,
                f"review {'ready' if self._review.ok else 'unavailable — ' + self._review.error}",
            )

        return analytics

    # ------------------------------------------------------------ OCR / frames

    async def ingest_frame(self, source: Any) -> FrameResult:
        """A camera frame arrives. Runs OCR, updates Merge Memory, reacts to a turn.

        This is the top of the chain and the only place a frame enters the
        system. Returns the `FrameResult` so the dashboard can show what happened
        to a frame that was rejected — "OCR ran and found nothing" and "OCR never
        ran" look the same from the outside otherwise.
        """

        if self.ocr is None:
            raise RuntimeError("No OCR pipeline configured for this session")

        if not self._state.camera_active:
            # Frames arriving while the camera is believed off means it came back.
            self.camera_on()

        result = self.ocr.update_memory(source)
        if not result.accepted:
            # A blurred or blank frame is routine. It is recorded, not raised:
            # the correct response is to wait for the next one.
            self._record(SessionEvent.CONTENT_UPDATED, f"frame ignored — {result.reason}")
            return result

        if result.page_changed:
            await self._page_turned(result)
            return result

        await self._apply_text(result.text, result.version)
        return result

    async def _page_turned(self, result: FrameResult) -> None:
        """OCR reported a new page. Decide, in order, what that means.

        The order matters and is the reason OCR reports rather than acts:

            1. commit the page being left, so it is in history before anything
               reads history
            2. move Merge Memory to the new page
            3. move the pointer, which is what tells Reading Speed to close its
               page observation
            4. rebuild the audio queue from the new text

        Doing 4 before 1 narrates the new page while the summary still thinks the
        reader is on the old one.
        """

        previous = self._state.pointer.page_index
        next_page = previous + 1

        self.memory.begin_page(next_page)
        self.memory.apply_frame(result.text, whole_page=True)
        self._content = self.memory.content_map(source_version=result.version)

        # Handed over before the pointer moves, and that order is the whole point.
        # `update_pointer` is what closes the page being left, and closing it is
        # what credits its words — so the map naming those words has to be in the
        # tracker first. Reversed, every page after the first closes with a word
        # count of zero: the tracker still holds the map from before the turn, in
        # which the page it is closing does not exist. Difficulty then rates each
        # page on time against no words and returns UNKNOWN forever.
        if self.speed.has(self.session_id):
            accepted = self.speed.update_content(self.session_id, self._content)
            if accepted:
                self._state = self._state.model_copy(
                    update={"content_version": self.memory.version}
                )

        self._record(SessionEvent.PAGE_CHANGED, f"{previous} -> {next_page}")

        await self.move_pointer(
            ReadingPointer(page_index=next_page, paragraph_index=0, sentence_index=0)
        )

        # No `ocr.reset()` here. The pipeline already replaced its words with the
        # new page's when it detected the turn — replacing rather than merging is
        # how it reports one — so those words are the new page, not leftovers from
        # the old one. Clearing them discards the only baseline the *next* turn
        # can be detected against: with nothing held, `detect_new_page` has no
        # vocabulary to compare and returns False, so page three reads as more of
        # page two and overwrites it. Every other turn is missed, and the skipped
        # page's text is lost.

    async def _apply_text(self, text: str, version: int) -> None:
        """CONTENT_UPDATED. Fold refined text into memory, then tell both consumers.

        Async only because of the audio refresh at the end. OCR keeps improving
        the same page as more frames arrive, so the paragraph the reader is
        hearing is not the paragraph Merge Memory now holds — without the
        refresh, narration would finish the page speaking the first frame's
        guess at it.
        """

        try:
            # `whole_page` because `text` is the OCR pipeline's merged page, not a
            # fragment of one: it already contains every word every frame of this
            # page has contributed. Appending it would add the page to itself
            # once per frame.
            self.memory.apply_frame(text, frame_version=version, whole_page=True)
        except StaleContentError as error:
            # Frames overtake each other over HTTP. Rejecting one is normal
            # operation, not a fault, and the session continues on the text it
            # already holds.
            self._record(SessionEvent.CONTENT_UPDATED, f"stale frame rejected — {error}")
            return

        content = self.memory.content_map(source_version=self.memory.version)
        self._content = content

        # Frames legitimately arrive before the reader presses start — the camera
        # streams as soon as the book is in view, and those early frames are what
        # give the session text to open with. Reading Speed has no session to
        # accept them into yet, so memory takes the text and the tracker is told
        # at `start_session` instead.
        if not self.speed.has(self.session_id):
            self._state = self._state.model_copy(
                update={"content_version": self.memory.version}
            )
            self._record(
                SessionEvent.CONTENT_UPDATED,
                f"v{self.memory.version} applied (pre-session)",
            )
            return

        accepted = self.speed.update_content(self.session_id, content)
        if accepted:
            self._state = self._state.model_copy(
                update={"content_version": self.memory.version}
            )

        self._record(
            SessionEvent.CONTENT_UPDATED,
            f"v{self.memory.version} {'applied' if accepted else 'rejected by reading speed'}",
        )

        if self.audio is not None and accepted:
            # The sentence in flight is already dequeued and is left alone; only
            # what the reader has not heard yet is rewritten. The version goes
            # with it so a frame delayed in transit cannot regress the queue.
            await self.audio.refresh_queue(
                text=self.current_text(), source_version=self.memory.version
            )

    # --------------------------------------------------------------- pointer

    async def move_pointer(
        self, pointer: ReadingPointer, *, corrected: bool = False
    ) -> None:
        """READING_POINTER_UPDATED. The single write path for the pointer.

        Gesture reports a position; this moves it. `corrected=True` marks the
        reader dragging the pointer back because it was wrong, which is friction
        Reading Speed counts. Ordinary forward movement is not friction.
        """

        previous_page = self._state.pointer.page_index
        self._state = self._state.model_copy(update={"pointer": pointer})

        if pointer.page_index == previous_page:
            self._record(
                SessionEvent.READING_POINTER_UPDATED,
                f"s{pointer.sentence_index}" + (" (correction)" if corrected else ""),
            )

        # Reading Speed infers a page change from the pointer, so a caller only
        # ever has to report a position.
        self.speed.update_pointer(self.session_id, pointer, corrected=corrected)
        if self.focus is not None:
            self.focus.pointer_updated(pointer)

        if self.audio is not None:
            if pointer.page_index != previous_page:
                # New page means new text from sentence zero: rebuild rather than
                # splice into a queue built from the previous page.
                await self.audio.start(pointer=pointer, text=self.current_text())
            else:
                await self.audio.seek(pointer=pointer, text=self.current_text())

    # --------------------------------------------------------------- gestures

    async def handle_gesture_event(
        self, event: SessionEvent, payload: dict[str, Any]
    ) -> None:
        """Receive what the Gesture pipeline published. The subscriber side of the rule.

        Gesture publishes and never calls, so this is where a published event
        becomes an action. Everything Gesture reports is advisory — this method
        is free to ignore any of it, and does: a `WORD_SELECTED` during Meaning
        Mode changes nothing, because the reader is already looking something up.
        """

        if event is SessionEvent.READING_POINTER_UPDATED:
            sentence = int(payload.get("sentence_index", self._state.pointer.sentence_index))
            paragraph = int(payload.get("paragraph_index", self._state.pointer.paragraph_index))
            await self.move_pointer(
                ReadingPointer(
                    page_index=self._state.pointer.page_index,
                    paragraph_index=max(0, paragraph),
                    sentence_index=max(0, sentence),
                ),
                corrected=bool(payload.get("corrected", False)),
            )

        elif event is SessionEvent.WORD_SELECTED:
            self._last_selected_word = str(payload.get("word", ""))
            self._record(SessionEvent.WORD_SELECTED, self._last_selected_word)

        elif event is SessionEvent.MEANING_REQUESTED:
            await self.meaning_mode_on(
                word=str(payload.get("word", "")),
                paragraph_index=payload.get("paragraph_index"),
            )

        elif event is SessionEvent.CAMERA_OFF:
            self.camera_off(str(payload.get("reason", "")))

    # ----------------------------------------------------------- meaning mode

    async def meaning_mode_on(
        self, *, word: str = "", paragraph_index: int | None = None
    ) -> None:
        """MEANING_MODE_ON. A pause that is a statement about the text.

        Counted apart from an ordinary pause because they mean opposite things: a
        user pause says nothing about the page, while Meaning Mode says the reader
        hit something they could not read past.

        `paragraph_index` is where the finger was, which `ReadingRuntime` resolves
        into Merge Memory's coordinates. It steers the explanation only: the
        friction is still charged to the paragraph the reader was *reading*, since
        reaching ahead to ask about a word is evidence about here, not about there.
        """

        if self._state.is_meaning_mode:
            return

        target = word or self._last_selected_word

        self._state = self._state.model_copy(update={"is_meaning_mode": True})
        self._record(SessionEvent.MEANING_MODE_ON, target)
        self.speed.meaning_mode(self.session_id, active=True)
        if self.focus is not None:
            self.focus.meaning_requested(target)
        if self.audio is not None:
            await self.audio.pause(reason=PauseReason.MEANING_MODE)

        # The model is asked only after narration has stopped. Asking first would
        # leave the reader hearing the next sentence for the several seconds a
        # round trip takes, having just gestured that they cannot read past this
        # word — the pause is the response to the gesture, and the explanation is
        # what fills it.
        if self.ai is not None and target:
            self._explanation = await self.ai.explain(
                word=target,
                paragraph=self.current_text(paragraph_index=paragraph_index),
                previous_paragraph=self._previous_text(paragraph_index=paragraph_index),
                page_number=self._state.pointer.page_index,
            )
            if self._explanation.ok:
                # Only a lookup that produced an answer counts as completed. A
                # failed call means the reader asked and got nothing, which is
                # not evidence about the page's difficulty.
                self.lookup_completed(target)
            else:
                self._record(
                    SessionEvent.MEANING_MODE_ON,
                    f"explanation unavailable — {self._explanation.error}",
                )

    async def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. Resumes the interrupted sentence, not the next one."""

        if not self._state.is_meaning_mode:
            return

        self._state = self._state.model_copy(update={"is_meaning_mode": False})
        self._record(SessionEvent.MEANING_MODE_OFF, "")
        self.speed.meaning_mode(self.session_id, active=False)
        if self.focus is not None:
            self.focus.meaning_mode_off()
        if self.audio is not None:
            await self.audio.resume()

    def lookup_completed(self, word: str = "") -> None:
        """LOOKUP_COMPLETED. Friction evidence, reported by the AI Engine.

        Not async and never sent to audio: a finished lookup changes nothing about
        playback, it only tells Reading Speed the reader needed help here.
        """

        target = word or self._last_selected_word
        if target:
            self._lookups.append(target)
        self._record(SessionEvent.LOOKUP_COMPLETED, target)
        self.speed.lookup_completed(self.session_id)
        if self.focus is not None:
            self.focus.lookup_completed(target)

    # ---------------------------------------------------------------- camera

    def camera_off(self, reason: str = "") -> None:
        """CAMERA_OFF. Not a pause: the reader keeps reading, we stop seeing them.

        The session clock keeps running deliberately. Stopping it would record a
        reader who read through a camera dropout as having taken a break, which
        deflates their measured pace by exactly the outage.
        """

        if not self._state.camera_active:
            return
        self._state = self._state.model_copy(update={"camera_active": False})
        self._record(SessionEvent.CAMERA_OFF, reason)

    def camera_on(self) -> None:
        """CAMERA_ON. Frames are arriving again."""

        if self._state.camera_active:
            return
        self._state = self._state.model_copy(update={"camera_active": True})
        self._record(SessionEvent.CAMERA_ON, "")

    # -------------------------------------------------------------- internals

    def _record(self, event: SessionEvent, detail: str) -> None:
        self._events.append(
            RuntimeEvent(event=event, detail=detail, at=self.clock())
        )
        logger.info(
            "[reading_engine:%s] %s%s",
            self.session_id,
            event.value,
            f" {detail}" if detail else "",
        )
