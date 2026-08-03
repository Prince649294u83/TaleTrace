"""Session progress tracking. The only mutable state in the module.

Everything else in `reading_speed` is a pure function; this is where the clocks
and counters live. Keeping mutation in exactly one file is what lets `predictor`
and `analytics` be trivially testable — they receive a snapshot and cannot alter
anything.

**Events are methods, not a bus.** The nine events the design calls for
(SESSION_STARTED, SESSION_PAUSED, SESSION_RESUMED, READING_POINTER_UPDATED,
PAGE_CHANGED, MEANING_MODE_ON, MEANING_MODE_OFF, LOOKUP_COMPLETED,
SESSION_FINISHED) are the nine methods below. This preserves the decoupling that
matters — the tracker never calls Gesture, OCR, AI, or the Audio Engine, and has
no idea which of them is talking to it — without introducing the only message bus
in the codebase. Nothing polls, either: callers report events when they happen.

**What this does not do**, and must never do: own the reading session, own the
reading pointer, or touch TTS. The pointer here is a *copy* of what the Reading
Engine owns, kept so predictions have a position to compare against. Writing to
it changes nothing about where the reader actually is.

Two clocks, matching the audio engine: the reading clock stops while paused or in
Meaning Mode, so looking up a word never makes the reader appear slower; the wall
clock runs from start to finish regardless.
"""

import logging
import time
from typing import Callable

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.reading_speed.analytics import PageObservation
from backend.app.modules.reading_speed.models import (
    ContentMap,
    ProgressSnapshot,
    ReadingMode,
)

logger = logging.getLogger(__name__)


class ProgressTracker:
    """Observed progress for one reading session.

    One tracker per session. The clock is injected so tests can advance time in
    controlled steps rather than sleeping, the same pattern the audio engine uses.
    """

    def __init__(
        self,
        session_id: str,
        reader_id: str,
        *,
        content: ContentMap | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_id = session_id
        self._reader_id = reader_id
        self._clock = clock
        self._content = content or ContentMap()

        self._mode = ReadingMode.FINISHED
        self._pointer = ReadingPointer()

        # Accumulated reading time, plus the instant the current reading
        # interval began. Splitting them this way means a pause only has to
        # bank the elapsed interval, and no timer needs cancelling.
        self._reading_ms = 0
        self._interval_started_at: float | None = None
        self._session_started_at: float | None = None
        self._finished_wall_ms: int | None = None

        self._lookups = 0
        self._meaning_modes = 0
        self._pointer_corrections = 0
        self._page_revisits = 0

        self._pages_seen: list[int] = []
        self._observations: list[PageObservation] = []
        self._page_started_reading_ms = 0
        self._page_start_words = 0
        self._origin_words = 0
        self._page_entry_pointer: ReadingPointer | None = None

    # ---------------------------------------------------------------- events

    def session_started(
        self, *, pointer: ReadingPointer | None = None, content: ContentMap | None = None
    ) -> None:
        """SESSION_STARTED. Resets every counter and starts both clocks."""

        if content is not None:
            self._content = content

        self._mode = ReadingMode.READING
        self._pointer = pointer or ReadingPointer()

        now = self._clock()
        self._reading_ms = 0
        self._interval_started_at = now
        self._session_started_at = now
        self._finished_wall_ms = None

        self._lookups = 0
        self._meaning_modes = 0
        self._pointer_corrections = 0
        self._page_revisits = 0

        self._pages_seen = [self._pointer.page_index]
        self._observations = []
        self._page_started_reading_ms = 0
        self._page_start_words = self._words_before(self._pointer)
        self._origin_words = self._page_start_words
        self._page_entry_pointer = self._pointer

        self._log("session_started", f"page={self._pointer.page_index}")

    def session_paused(self) -> None:
        """SESSION_PAUSED. Banks the reading interval; the wall clock runs on."""

        self._suspend(ReadingMode.PAUSED)

    def session_resumed(self) -> None:
        """SESSION_RESUMED. Restarts the reading clock from now."""

        if self._mode is ReadingMode.FINISHED:
            return
        if self._mode is ReadingMode.READING:
            return
        self._mode = ReadingMode.READING
        self._interval_started_at = self._clock()
        self._log("session_resumed", f"reading_ms={self._reading_ms}")

    def meaning_mode_on(self) -> None:
        """MEANING_MODE_ON. A pause that is also a difficulty signal.

        Counted separately from a plain pause because the two mean opposite
        things about the page: a user pause says nothing about the text, while
        entering Meaning Mode says the reader hit something they could not read
        past. That count is what stops `analytics` from mistaking an
        interruption for a hard page.
        """

        already_counted = self._mode is ReadingMode.MEANING_MODE
        self._suspend(ReadingMode.MEANING_MODE)
        if not already_counted:
            self._meaning_modes += 1

    def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. Back to reading."""

        if self._mode is ReadingMode.MEANING_MODE:
            self.session_resumed()

    def lookup_completed(self) -> None:
        """LOOKUP_COMPLETED. The reader asked what a word meant and got an answer.

        Recorded whatever the current mode is: a lookup that happened while
        paused is still a lookup, and dropping it would understate the friction
        on the page.
        """

        self._lookups += 1
        self._log("lookup_completed", f"total={self._lookups}")

    def pointer_updated(self, pointer: ReadingPointer, *, corrected: bool = False) -> None:
        """READING_POINTER_UPDATED. Copy a new position from the pointer's owner.

        Gesture and the Reading Engine both land here. `corrected=True` marks a
        gesture correction — the reader dragging the pointer back because it was
        in the wrong place — which is friction evidence. Normal forward
        advancement is not.

        A pointer that moves to a different page implies a page turn, so this
        forwards to `page_changed()` rather than letting the page counters drift
        out of step with the pointer.
        """

        if pointer.page_index != self._pointer.page_index:
            self.page_changed(pointer)
        else:
            self._pointer = pointer

        if corrected:
            self._pointer_corrections += 1

    def page_changed(self, pointer: ReadingPointer) -> None:
        """PAGE_CHANGED. Close the page just left, open the next.

        Closing a page is what produces a `PageObservation`, and observations are
        what difficulty analysis consumes. A session that never turns a page
        therefore has no per-page difficulty — correctly, since there is nothing
        to compare.
        """

        leaving = self._pointer.page_index
        arriving = pointer.page_index

        if arriving != leaving:
            self._close_page(leaving, completed=arriving > leaving)
            if arriving in self._pages_seen:
                self._page_revisits += 1
            else:
                self._pages_seen.append(arriving)

        self._pointer = pointer
        self._page_entry_pointer = pointer
        self._page_started_reading_ms = self._elapsed_reading_ms()
        self._page_start_words = self._words_before(pointer)
        self._log("page_changed", f"{leaving}->{arriving}")

    def session_finished(self) -> None:
        """SESSION_FINISHED. Stop both clocks and close the final page.

        Idempotent, because the audio engine reaching FINISHED and the reader
        closing the book both legitimately call this and neither knows about the
        other.
        """

        if self._mode is ReadingMode.FINISHED:
            return

        self._bank_interval()
        # Not a forward turn: the reader stopped where they stopped, so the last
        # page is credited only as far as the pointer actually reached.
        self._close_page(self._pointer.page_index, completed=False)
        self._finished_wall_ms = self._elapsed_wall_ms()
        self._mode = ReadingMode.FINISHED
        self._log(
            "session_finished",
            f"reading_ms={self._reading_ms} pages={len(self._observations)}",
        )

    # ----------------------------------------------------------------- reads

    def snapshot(self) -> ProgressSnapshot:
        """Current progress. Cheap enough to call on every request.

        Computed rather than stored, so the reading clock is always current
        without a background task ticking it forward.
        """

        return ProgressSnapshot(
            session_id=self._session_id,
            reader_id=self._reader_id,
            pointer=self._pointer,
            mode=self._mode,
            elapsed_reading_ms=self._elapsed_reading_ms(),
            elapsed_wall_ms=self._elapsed_wall_ms(),
            words_confirmed=self._words_before(self._pointer),
            pages_visited=len(self._pages_seen),
            lookup_count=self._lookups,
            meaning_mode_count=self._meaning_modes,
            pointer_corrections=self._pointer_corrections,
            page_revisits=self._page_revisits,
        )

    @property
    def observations(self) -> list[PageObservation]:
        """Closed pages, in the order they were left."""

        return list(self._observations)

    @property
    def content(self) -> ContentMap:
        return self._content

    @property
    def origin_word_offset(self) -> int:
        """Words before where this session began.

        Predictions need it: a reader who opened the book at page 40 has not read
        the preceding 39 pages, and measuring them against word zero would report
        them permanently and absurdly behind.
        """

        return self._origin_words

    def set_content(self, content: ContentMap) -> bool:
        """Adopt a new content map from Merge Memory, if it is not stale.

        Merge Memory stays the source of truth — this only ever reads from it.
        Older versions are rejected because OCR updates arrive over HTTP and can
        overtake each other; applying a stale map would shrink the word counts
        under a prediction already in flight.

        Returns False when rejected. Rejection is normal, not an error.
        """

        if content.source_version < self._content.source_version:
            self._log(
                "content_rejected",
                f"version={content.source_version} current={self._content.source_version}",
            )
            return False
        self._content = content
        return True

    # ------------------------------------------------------------- internals

    def _suspend(self, mode: ReadingMode) -> None:
        """Stop the reading clock and enter a non-reading mode."""

        if self._mode is ReadingMode.FINISHED:
            return
        self._bank_interval()
        self._mode = mode
        self._log(mode.value, f"reading_ms={self._reading_ms}")

    def _bank_interval(self) -> None:
        """Add the open reading interval to the total and close it."""

        if self._interval_started_at is None:
            return
        self._reading_ms += int((self._clock() - self._interval_started_at) * 1000)
        self._interval_started_at = None

    def _elapsed_reading_ms(self) -> int:
        """Banked reading time plus any interval still open."""

        if self._interval_started_at is None:
            return self._reading_ms
        return self._reading_ms + int((self._clock() - self._interval_started_at) * 1000)

    def _elapsed_wall_ms(self) -> int:
        if self._session_started_at is None:
            return 0
        if self._finished_wall_ms is not None:
            return self._finished_wall_ms
        return int((self._clock() - self._session_started_at) * 1000)

    def _close_page(self, page_index: int, *, completed: bool = True) -> None:
        """Record what happened on a page as it is left.

        Per-page friction is derived by difference from the session totals, so a
        lookup is attributed to the page the reader was on when it happened
        without every counter needing a per-page copy.

        `completed` says whether the reader left forwards. Word count cannot come
        from the pointer alone: a reader who turns from page 1 to page 2 read all
        of page 1, but the pointer commonly still sits mid-page because Gesture
        only reports a position when the reader moves it, not once per sentence.
        Trusting the pointer there would credit a fraction of the words against
        the full page time and rate every ordinary page as difficult. So a
        forward turn counts the whole page; going backwards counts only as far as
        the pointer actually reached.
        """

        reading_ms = self._elapsed_reading_ms() - self._page_started_reading_ms
        reached = max(self._words_before(self._pointer) - self._page_start_words, 0)
        page_total = self._page_word_total(page_index)

        if completed:
            words = page_total or reached
        else:
            words = reached or page_total

        prior = self._observation_for(page_index)
        seen_lookups = sum(o.lookups for o in self._observations)
        seen_meaning = sum(o.meaning_requests for o in self._observations)
        seen_corrections = sum(o.pointer_corrections for o in self._observations)

        observation = PageObservation(
            page_index=page_index,
            words=words,
            reading_ms=max(reading_ms, 0),
            lookups=max(self._lookups - seen_lookups, 0),
            meaning_requests=max(self._meaning_modes - seen_meaning, 0),
            pointer_corrections=max(self._pointer_corrections - seen_corrections, 0),
            revisits=1 if prior is not None else 0,
        )
        self._observations.append(observation)

    def _page_word_total(self, page_index: int) -> int:
        """Words on `page_index` from where the reader entered it onward.

        Not the page's full word count: a session that opens mid-page, or a
        gesture that lands halfway down one, did not cover what came before the
        entry point, and crediting those words would report a pace nobody read at.

        Falls back to `page_word_counts` when the page has no sentence spans,
        which is normal for a page OCR has counted but not yet segmented.
        """

        spans = [s for s in self._content.sentences if s.pointer.page_index == page_index]
        if not spans:
            return max(self._content.page_word_counts.get(page_index, 0), 0)

        entry = self._page_entry_pointer
        if entry is None or entry.page_index != page_index:
            return sum(s.word_count for s in spans)

        key = entry.sentence_order_key()
        return sum(s.word_count for s in spans if s.pointer.sentence_order_key() >= key)

    def _observation_for(self, page_index: int) -> PageObservation | None:
        for observation in self._observations:
            if observation.page_index == page_index:
                return observation
        return None

    def _words_before(self, pointer: ReadingPointer) -> int:
        return self._content.words_before(pointer)

    def _log(self, event: str, detail: str = "") -> None:
        """One structured line through the app's logging config.

        Same shape as the audio engine's `[audio:<session>]` lines, for the same
        reason: this module adds no diagnostics system of its own.
        """

        logger.info(
            "[reading_speed:%s] %s%s",
            self._session_id,
            event,
            f" {detail}" if detail else "",
        )
