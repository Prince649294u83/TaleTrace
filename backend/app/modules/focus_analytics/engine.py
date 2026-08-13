"""Reading Focus Analysis Engine. The only mutable state in the module.

Sits beside Reading Speed and observes. It is fed events and answers one question
at the end of a session:

    which sections required more attention than expected?

**Not** "was the reader distracted?" — that question cannot be answered from a
pointer and a clock, and this engine is built so that it cannot accidentally
appear to answer it. Where the evidence supports only "the pointer stopped
moving", the report says *possible idle time*, and the paragraph is rated UNKNOWN
rather than difficult.

It controls nothing
-------------------
No method on this class calls OCR, the AI Engine, the Audio Engine, Merge Memory's
write side, the Reading Pointer or Reading Speed. It has no `set_`, no `advance`,
no `seek`. Every dependency points inward: things report to it, it reports to
nobody. A future change that wants this engine to slow playback down on a hard
paragraph is a change to the Reading Engine, which can read this engine's output
and decide — the decision is not this module's to make.

Nothing polls
-------------
There is no timer and no background task. Time advances only when an event
arrives, which is what lets a fifteen-minute session be tested in a millisecond
by handing the engine timestamps. The clock is injected for exactly that reason.

It never reconstructs text
--------------------------
Paragraph structure is *asked for*, never derived. Word counts come from Merge
Memory's own `content_map()`, grouped by the paragraph index the spans already
carry. This engine never splits a string, never counts words in text it holds,
and in fact never holds text at all — a second opinion about where a paragraph
begins is exactly the drift the Merge Memory contract exists to prevent.

Two clocks, matching Reading Speed
----------------------------------
The reading clock stops while paused or in Meaning Mode; the wall clock does not.
Looking a word up must never make the reader appear slower — it is already
counted as friction, and letting it count as time too would score the same moment
of confusion twice.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.focus_analytics.analysis import assess_paragraph, idle_ms_for_gap
from backend.app.modules.focus_analytics.models import (
    FocusReport,
    ParagraphFocus,
    ParagraphObservation,
)
from backend.app.modules.reading_speed.models import (
    ContentMap,
    DEFAULT_BASELINE_WPM,
    ReadingBaseline,
)
from backend.app.shared.events import SessionEvent
from backend.app.shared.merge_memory import MergeMemorySource

logger = logging.getLogger(__name__)


class _Tally:
    """Running counts for one paragraph. Internal; becomes a frozen observation."""

    __slots__ = ("actual_ms", "idle_ms", "meaning_requests", "lookups", "visits")

    def __init__(self) -> None:
        self.actual_ms = 0
        self.idle_ms = 0
        self.meaning_requests = 0
        self.lookups = 0
        self.visits = 0


class FocusAnalyticsEngine:
    """Per-paragraph attention analysis for one reading session.

    One instance per session. `memory` is the read side of Merge Memory and is
    optional only so that the engine can be driven by injected structure in tests
    — in production it is always the session's real memory, and without it the
    engine has no word counts and rates everything UNKNOWN rather than guessing.
    """

    def __init__(
        self,
        session_id: str,
        reader_id: str,
        *,
        memory: MergeMemorySource | None = None,
        baseline: ReadingBaseline | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_id = session_id
        self._reader_id = reader_id
        self._memory = memory
        self._clock = clock
        self._baseline = baseline or ReadingBaseline(
            reader_id=reader_id, baseline_wpm=DEFAULT_BASELINE_WPM
        )

        self._tallies: dict[tuple[int, int], _Tally] = {}
        # Insertion order is reading order, and dicts preserve it. Reports are
        # emitted in the order the reader met the paragraphs rather than sorted
        # by index, because a gesture jump backwards is part of what happened.
        self._order: list[tuple[int, int]] = []

        self._pointer: ReadingPointer | None = None
        self._current: tuple[int, int] | None = None
        self._entered_at_ms = 0
        self._last_move_ms = 0
        self._words_at_last_move = 0

        self._reading_ms = 0
        self._interval_started_at: float | None = None
        self._running = False
        self._finished = False

        self._content: ContentMap | None = None
        self._content_version = -1
        self._structure: dict[tuple[int, int], int] = {}

    # ---------------------------------------------------------------- events

    def session_started(self, pointer: ReadingPointer | None = None) -> None:
        """SESSION_STARTED. Clears every tally and opens the first paragraph."""

        self._tallies.clear()
        self._order.clear()
        self._reading_ms = 0
        self._interval_started_at = self._clock()
        self._running = True
        self._finished = False

        self._pointer = pointer or ReadingPointer()
        self._refresh_structure()
        self._enter(self._key(self._pointer))
        self._log("session_started")

    def pointer_updated(self, pointer: ReadingPointer) -> None:
        """READING_POINTER_UPDATED. The only event that can produce idle time.

        A gap between pointer updates is measured against the words those updates
        crossed, so the same ten seconds is unremarkable across a long paragraph
        and idle across three words. Charging a flat threshold would report every
        dense paragraph as an interruption.
        """

        if not self._running or self._finished:
            return

        now_ms = self._elapsed_ms()
        words_now = self._words_before(pointer)
        gap_ms = now_ms - self._last_move_ms
        crossed = max(words_now - self._words_at_last_move, 0)

        idle = idle_ms_for_gap(gap_ms, crossed, self._baseline)
        if idle and self._current is not None:
            self._tally(self._current).idle_ms += idle

        key = self._key(pointer)
        if key != self._current:
            self._leave(now_ms)
            self._enter(key, now_ms=now_ms)

        self._pointer = pointer
        self._last_move_ms = now_ms
        self._words_at_last_move = words_now

    def page_changed(self, pointer: ReadingPointer) -> None:
        """PAGE_CHANGED. A page turn is a paragraph change with a new page index.

        Forwarded rather than handled separately: the paragraph the reader was in
        has ended either way, and a second closing path would be a second chance
        for the tallies to disagree with the pointer.
        """

        self.pointer_updated(pointer)

    def meaning_requested(self, word: str = "") -> None:
        """MEANING_REQUESTED / MEANING_MODE_ON. Friction, and the clock stops.

        Counted against the paragraph the reader is *in*, not the one containing
        the word, because the two are the same in every case the hardware can
        produce and the pointer is the only one of them this engine is told about.
        """

        if self._current is not None:
            self._tally(self._current).meaning_requests += 1
        self._suspend()
        self._log("meaning_requested", word)

    def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. The reading clock restarts from now."""

        self._resume()

    def lookup_completed(self, word: str = "") -> None:
        """LOOKUP_COMPLETED. Evidence, but not a second count against the score.

        A completed lookup is the resolution of a meaning request, not a separate
        moment of confusion, so it appears in the evidence and not in the
        Revision Priority Score.
        """

        if self._current is not None:
            self._tally(self._current).lookups += 1
        self._log("lookup_completed", word)

    def session_paused(self) -> None:
        """SESSION_PAUSED. Banks the reading interval."""

        self._suspend()

    def session_resumed(self) -> None:
        """SESSION_RESUMED. Restarts the reading clock.

        The gap is not idle time. The reader told the system they were stopping,
        and time a reader explicitly paused is not time they were expected to be
        reading.
        """

        self._resume()

    def session_finished(self) -> None:
        """SESSION_FINISHED. Closes the final paragraph. Idempotent."""

        if self._finished:
            return

        self._close_trailing_gap()
        now_ms = self._elapsed_ms()
        self._bank()
        self._leave(now_ms)
        self._running = False
        self._finished = True
        self._log("session_finished", f"paragraphs={len(self._order)}")

    def _close_trailing_gap(self) -> None:
        """Attribute idle time for the last paragraph, which no pointer move closes.

        Every other paragraph's idle time is measured when the *next* pointer
        update arrives and reveals how much text the gap covered. The final
        paragraph has no next update, so without this a reader who stalled for
        five minutes and then closed the book would have all five charged as
        reading time — and the last paragraph of every interrupted session would
        be the slowest thing in the report.

        The text crossed is taken as the whole paragraph, which is the most
        generous reading available: the reader may have got no further than the
        first line, but assuming they finished is the assumption least likely to
        invent idle time that was really reading.
        """

        if not self._running or self._current is None:
            return

        gap_ms = self._elapsed_ms() - self._last_move_ms
        idle = idle_ms_for_gap(gap_ms, self._structure.get(self._current, 0), self._baseline)
        if idle:
            self._tally(self._current).idle_ms += idle

    def handle(self, event: SessionEvent, pointer: ReadingPointer | None = None) -> None:
        """Dispatch a session event. The wiring seam for the Reading Engine.

        Unknown and irrelevant events are ignored rather than raising: this engine
        observes a stream it does not own, and a new event elsewhere in the system
        must never be able to stop a session by arriving here.
        """

        if event is SessionEvent.SESSION_STARTED:
            self.session_started(pointer)
        elif event is SessionEvent.READING_POINTER_UPDATED and pointer is not None:
            self.pointer_updated(pointer)
        elif event is SessionEvent.PAGE_CHANGED and pointer is not None:
            self.page_changed(pointer)
        elif event in (SessionEvent.MEANING_REQUESTED, SessionEvent.MEANING_MODE_ON):
            self.meaning_requested()
        elif event is SessionEvent.MEANING_MODE_OFF:
            self.meaning_mode_off()
        elif event is SessionEvent.LOOKUP_COMPLETED:
            self.lookup_completed()
        elif event is SessionEvent.SESSION_PAUSED:
            self.session_paused()
        elif event is SessionEvent.SESSION_RESUMED:
            self.session_resumed()
        elif event is SessionEvent.SESSION_FINISHED:
            self.session_finished()

    # ----------------------------------------------------------------- reads

    @property
    def baseline(self) -> ReadingBaseline:
        return self._baseline

    def observations(self) -> list[ParagraphObservation]:
        """Every paragraph the reader entered, in the order they met them.

        Includes the paragraph currently open, so a report taken mid-session is
        complete as far as it goes rather than missing the paragraph the reader is
        actually on — which is the one a live view most wants to show.
        """

        now_ms = self._elapsed_ms()
        out: list[ParagraphObservation] = []

        for key in self._order:
            tally = self._tallies[key]
            page_index, paragraph_index = key
            actual = tally.actual_ms
            if key == self._current and not self._finished:
                actual += max(now_ms - self._entered_at_ms, 0)

            out.append(
                ParagraphObservation(
                    page_index=page_index,
                    paragraph_index=paragraph_index,
                    words=self._structure.get(key, 0),
                    actual_ms=actual,
                    idle_ms=min(tally.idle_ms, actual),
                    meaning_requests=tally.meaning_requests,
                    lookups=tally.lookups,
                    revisits=max(tally.visits - 1, 0),
                )
            )
        return out

    def report(self, baseline: ReadingBaseline | None = None) -> FocusReport:
        """The session's focus analysis. Computes nothing it has not observed.

        `baseline` overrides the one held, so a session can be re-judged against a
        calibration that arrived after it ended without replaying the events. This
        never *stores* the override: moving a baseline is `calibration.adapt()`'s
        decision, and making it a side effect of asking for a report would mean
        reading a report twice changed the reader's profile.
        """

        against = baseline or self._baseline
        paragraphs: list[ParagraphFocus] = [
            assess_paragraph(observation, against) for observation in self.observations()
        ]

        return FocusReport(
            session_id=self._session_id,
            reader_id=self._reader_id,
            baseline_wpm=against.baseline_wpm,
            baseline_was_evidence=against.is_evidence,
            paragraphs=tuple(paragraphs),
            total_idle_ms=sum(p.idle_ms for p in paragraphs),
            total_focused_ms=sum(p.focused_ms for p in paragraphs),
            total_words=sum(p.words for p in paragraphs),
        )

    # -------------------------------------------------------------- internals

    def _key(self, pointer: ReadingPointer) -> tuple[int, int]:
        return (pointer.page_index, pointer.paragraph_index)

    def _tally(self, key: tuple[int, int]) -> _Tally:
        tally = self._tallies.get(key)
        if tally is None:
            tally = _Tally()
            self._tallies[key] = tally
            self._order.append(key)
        return tally

    def _enter(self, key: tuple[int, int], *, now_ms: int | None = None) -> None:
        tally = self._tally(key)
        tally.visits += 1
        self._current = key
        self._entered_at_ms = self._elapsed_ms() if now_ms is None else now_ms
        self._last_move_ms = self._entered_at_ms

    def _leave(self, now_ms: int) -> None:
        if self._current is None:
            return
        self._tally(self._current).actual_ms += max(now_ms - self._entered_at_ms, 0)
        self._current = None

    def _suspend(self) -> None:
        if not self._running:
            return
        self._bank()
        self._running = False

    def _resume(self) -> None:
        if self._finished or self._running:
            return
        self._interval_started_at = self._clock()
        self._running = True
        # Nothing else needs adjusting, and that is the payoff of measuring in
        # reading time rather than wall time: the pause advanced the reading clock
        # by zero, so the open paragraph was not charged for it and the gap since
        # the last pointer move did not grow. A wall-clock version of this engine
        # would have to correct both here, and forgetting either would report
        # every pause as idle time.
        #
        # In particular `_last_move_ms` is deliberately left alone. Moving it to
        # now would forgive whatever gap had already accumulated before the pause,
        # so a reader who stalled for a minute and *then* took a break would have
        # the stall erased by the break — and the way to hide idle time from this
        # report would be to pause the session, which is the one thing a reader
        # pausing is not saying.

    def _bank(self) -> None:
        if self._interval_started_at is None:
            return
        self._reading_ms += int((self._clock() - self._interval_started_at) * 1000)
        self._interval_started_at = None

    def _elapsed_ms(self) -> int:
        if self._running and self._interval_started_at is not None:
            return self._reading_ms + int((self._clock() - self._interval_started_at) * 1000)
        return self._reading_ms

    def _refresh_structure(self) -> None:
        """Ask Merge Memory for paragraph word counts. Cached on its version.

        Grouping the content map's sentence spans by the paragraph index they
        already carry is the whole of the "which paragraph contains this
        sentence?" question — the spans were built by Merge Memory's own
        segmenter, so the answer cannot drift from the one the Audio Engine and
        Reading Speed are using.
        """

        if self._memory is None:
            return
        version = self._memory.version
        if self._content is not None and version == self._content_version:
            return

        content = self._memory.content_map()
        structure: dict[tuple[int, int], int] = {}
        for span in content.sentences:
            key = (span.pointer.page_index, span.pointer.paragraph_index)
            structure[key] = structure.get(key, 0) + span.word_count

        self._content = content
        self._content_version = version
        self._structure = structure

    def _words_before(self, pointer: ReadingPointer) -> int:
        self._refresh_structure()
        if self._content is None:
            return 0
        return self._content.words_before(pointer)

    def _log(self, event: str, detail: str = "") -> None:
        logger.info(
            "[focus_analytics:%s] %s%s", self._session_id, event, f" {detail}" if detail else ""
        )
