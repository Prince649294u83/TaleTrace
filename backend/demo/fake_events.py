"""The scripted timeline for the Reading Speed rehearsal.

Kept apart from rendering and from the fakes so the sequence of events reads as a
sequence of events. Every step here is something *another* module would report:
Gesture moving a pointer, the AI engine finishing a lookup, the Reading Engine
turning a page. Reading Speed originates none of it.

The timeline is scripted to produce a known outcome, so the checklist at the end
can assert specific results rather than "something happened":

    page 1  on pace          -> expect LOW, small deviation
    page 2  slow + lookups   -> expect MEDIUM or HIGH, corroborated
    page 3  slow, no lookups -> expect UNKNOWN (an interruption, not difficulty)

Page 3 is the important one. It is the case where time alone would say "hard" and
the module must refuse to.
"""

import time
from dataclasses import dataclass, field
from typing import Callable

from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.demo import reading_speed_console as view
from backend.demo.fake_reader import ScriptedReader

SESSION_ID = "rehearsal"
READER_ID = "demo-reader"


@dataclass
class Timeline:
    """Drives one rehearsal from start to finish.

    `sleep` and `clock` are injected so the same script runs compressed (instant,
    usable as a smoke test) or in realtime (watchable). Compressed runs advance a
    fake clock; realtime runs use the wall clock and actually wait.
    """

    service: ReadingSpeedService
    reader: ScriptedReader
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    sample_every_ms: int = 1_000
    # Scripted steps are keyed by sentence index, and a gesture correction moves
    # the index backwards — so without this the reader re-reaches the trigger and
    # jumps back forever. Each step fires once per run.
    _fired: set[int] = field(default_factory=set)

    def run(self) -> None:
        content = self.reader.content

        view.event(
            "SESSION_STARTED",
            f"{content.sentence_count} sentences over {len(content.page_word_counts)} pages",
        )
        self.service.start_session(
            session_id=SESSION_ID, reader_id=READER_ID, content=content
        )
        view.header()

        page = self.reader.page_index
        while not self.reader.finished:
            self._read_sentence()

            if self.reader.page_index != page:
                page = self.reader.page_index
                view.event("PAGE_CHANGED", f"reader turned to page {page}")

            self._scripted_interruptions()

        view.event("SESSION_FINISHED", "reader closed the book")

    # ------------------------------------------------------------------ steps

    def _read_sentence(self) -> None:
        """Spend a sentence's worth of time, sampling the prediction as we go.

        Sampled at 1 Hz because that is the cadence a UI would redraw at — and it
        demonstrates the point that no loop is needed to get a current answer.
        Each sample is an ordinary read; nothing is cached between them.
        """

        duration_ms = self.reader.next_sentence_ms()
        elapsed = 0
        while elapsed < duration_ms:
            step = min(self.sample_every_ms, duration_ms - elapsed)
            self.sleep(step / 1000.0)
            elapsed += step
            self._sample()

        pointer = self.reader.advance()
        self.service.update_pointer(SESSION_ID, pointer)

    def _sample(self) -> None:
        snapshot = self.service.tracker(SESSION_ID).snapshot()
        view.tick(
            snapshot,
            self.service.predict(SESSION_ID),
            observed_wpm=self.service.observed_wpm(SESSION_ID),
        )

    def _due(self, index: int) -> bool:
        """Whether the step keyed to `index` still needs to fire."""

        if self.reader.index != index or index in self._fired:
            return False
        self._fired.add(index)
        return True

    def _scripted_interruptions(self) -> None:
        """Fire the events that make this a rehearsal rather than a stopwatch."""

        # Page 2: the reader hits a hard word. Meaning Mode freezes the reading
        # clock, and the lookup is the friction that corroborates a slow page.
        if self._due(10):
            view.event("MEANING_MODE_ON", "reader asked what a word means")
            self.service.meaning_mode(SESSION_ID, active=True)
            snapshot = self.service.tracker(SESSION_ID).snapshot()
            view.mode_line(snapshot, tts=False)

            self.sleep(6.0)  # the AI engine takes a while to answer

            view.event("LOOKUP_COMPLETED", "explanation delivered")
            self.service.lookup_completed(SESSION_ID)
            view.event("MEANING_MODE_OFF", "back to reading")
            self.service.meaning_mode(SESSION_ID, active=False)
            self._show_frozen_clock()

        if self._due(13):
            view.event("LOOKUP_COMPLETED", "second lookup on the same page")
            self.service.lookup_completed(SESSION_ID)

        # Page 2: the reader loses their place and drags the pointer back. A
        # correction is friction; ordinary forward movement is not.
        if self._due(15):
            pointer = self.reader.jump_back(2)
            view.event(
                "READING_POINTER_UPDATED",
                f"gesture correction back to sentence {pointer.sentence_index}",
            )
            self.service.update_pointer(SESSION_ID, pointer, corrected=True)

        # Page 3: a long pause with no lookups at all. This is the case the module
        # must NOT call difficult — the reader walked away.
        if self._due(20):
            view.event("SESSION_PAUSED", "reader set the book down")
            self.service.pause(SESSION_ID)
            self._show_frozen_clock()
            self.sleep(20.0)
            view.event("SESSION_RESUMED", "reader picked it back up")
            self.service.resume(SESSION_ID)

        # A refreshed OCR map arriving mid-session, and a stale one behind it.
        if self._due(24):
            fresh = self.reader.content.model_copy(update={"source_version": 9})
            applied = self.service.update_content(SESSION_ID, fresh)
            view.event("CONTENT_UPDATED", f"merge memory v9 applied={applied}")

            stale = self.reader.content.model_copy(update={"source_version": 4})
            rejected = not self.service.update_content(SESSION_ID, stale)
            view.event(
                "CONTENT_UPDATED",
                f"stale merge memory v4 rejected={rejected} (normal, not an error)",
            )

    def _show_frozen_clock(self) -> None:
        snapshot = self.service.tracker(SESSION_ID).snapshot()
        view.mode_line(snapshot, tts=False)


def realtime_timeline(service: ReadingSpeedService, reader: ScriptedReader) -> Timeline:
    """A timeline that runs at human speed against the wall clock."""

    return Timeline(service=service, reader=reader, sleep=time.sleep, clock=time.monotonic)


class FakeClock:
    """A clock the timeline advances instead of waiting.

    Lets the identical script run as a fast smoke test. Compressed time is not a
    substitute for the realtime run — the audio engine's dead sink survived every
    compressed run and died in seconds against real playback — but for a module
    with no I/O it is the same code path, only quicker.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def compressed_timeline(reader: ScriptedReader) -> tuple[ReadingSpeedService, Timeline]:
    clock = FakeClock()
    service = ReadingSpeedService(clock=clock)
    timeline = Timeline(service=service, reader=reader, sleep=clock.sleep, clock=clock)
    return service, timeline
