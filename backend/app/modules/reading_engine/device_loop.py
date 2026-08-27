"""The device control loop: ESP32 rig driving one reading session.

Migrated from `main_loop` in `OCRandGESTURE/main_controller.py`. The reference
did the right things in the right order — poll buttons first, let the toggle
pre-empt everything, treat continuous OCR as the default state — and that
sequence is preserved exactly. What changed is what it does with what it finds:

    reference   button pressed -> call gesture engine -> print result
    here        button pressed -> publish event       -> runtime handles it

so the loop is testable against a fake device, and no hardware call reaches into
the Audio or AI engines.

    ESP32 buttons ─events─┐
                          v
    ESP32-CAM ─frames─> ReadingRuntime ─> OCR ─> Merge Engine ─> Merge Memory
                                              ─> Gesture ─> Reading ─> Audio ─> AI

The one behaviour worth naming
------------------------------
The reference held execution inside a nested `while True` while the Meaning Mode
toggle was on, so OCR stopped and resumed when the switch went off. That is a
real behaviour, not an accident: the reader has stopped reading and is asking
about a word, and merging frames of a page they are pointing at rather than
reading would advance the pointer past where they are.

It is preserved here without the nested loop. `Esp32Buttons.poll` reports edges,
so the toggle staying on produces no further events, and this loop simply skips
frame capture while `meaning_mode` is true. Same behaviour, one loop, and a test
can step it.

Hardware Mode and Simulation Mode
---------------------------------
The two differ in exactly three fields — `camera`, `buttons`, `clock` — and in
nothing else. Everything downstream of `tick()` is the same object graph running
the same code in the same order, which is what makes a simulated session evidence
about the real one rather than a separate thing that resembles it. When the rig
arrives, the swap is at construction and this file does not change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.image_receiver import Esp32Buttons, Esp32Camera
from backend.app.modules.image_receiver.protocols import ButtonSource, CameraSource, DisplayTarget
from backend.app.modules.reading_engine.engine import MeaningLookupResult
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.shared.clock import Clock, RealClock
from backend.app.shared.events import SessionEvent

logger = logging.getLogger(__name__)

# The reference's loop delay. Fast enough that a button press is never missed,
# slow enough that an idle rig is not hammered.
_TICK_SECONDS = 0.1

# How often the page is re-read, as opposed to how often the buttons are polled.
#
# The reference had no such separation: `main_loop` slept 0.1s and called
# `process_frame` on every pass, and `process_frame` is a Google Vision call plus
# two Groq calls. That is ~600 Vision and ~1200 Groq calls per minute of reading,
# for a page that changes when a human turns it — a few times a minute at most.
#
# This is one of the few places the port deliberately departs from the reference,
# because the standing cost requirement (Vision processes each image once) and the
# reference's behaviour cannot both be honoured, and the reference is wrong on the
# merits: 99% of those calls re-read a page that has not changed, and each one is
# an opportunity for the merge to corrupt text that was already correct.
#
# Buttons stay at 0.1s, so responsiveness is unchanged — which is the half of the
# reference's cadence that was actually about the reader.
_OCR_INTERVAL_SECONDS = 1.0

# After a button press the reference waited a second before capturing, to let the
# reader settle their hand on the word. Removing it makes selection land on
# whatever the finger was passing over.
_GESTURE_SETTLE_SECONDS = 1.0


@dataclass
class DeviceLoop:
    """One reading session driven by the ESP32 rig.

    Owns no reading state: every decision belongs to `ReadingRuntime` and the
    modules under it. This turns hardware into events and frames, in order.
    """

    runtime: ReadingRuntime
    camera: CameraSource = field(default_factory=Esp32Camera)
    buttons: ButtonSource = field(default_factory=Esp32Buttons)
    display: DisplayTarget | None = None
    settle_seconds: float = _GESTURE_SETTLE_SECONDS
    # The only reason a simulated session can cover fifteen minutes in seconds.
    # Real by default so production and `live_session` are unaffected by its
    # existence; a `VirtualClock` here scales the waiting without scaling the
    # session time the analytics are computed from.
    clock: Clock = field(default_factory=RealClock)
    tick_seconds: float = _TICK_SECONDS
    # Seconds between OCR passes. 0.0 restores the reference's every-tick
    # behaviour, which the differential harness uses to compare like with like.
    ocr_interval_seconds: float = _OCR_INTERVAL_SECONDS

    frames_processed: int = field(default=0, init=False)
    frames_skipped: int = field(default=0, init=False)
    gestures_run: int = field(default=0, init=False)
    events_published: list[SessionEvent] = field(default_factory=list, init=False)
    _next_ocr_at: float | None = field(default=None, init=False)

    async def tick(self) -> list[SessionEvent]:
        """One pass of the control loop. Returns the events this pass produced.

        Order matches the reference: buttons are read before frames, so a press
        is acted on with the frame taken *after* it rather than one taken before
        the reader had pointed at anything.
        """

        events = self.buttons.poll()
        self.events_published.extend(events)

        for event in events:
            await self._handle(event)

        # Meaning Mode holds the reading loop, exactly as the reference's nested
        # wait did. The reader is asking about a word, not reading on.
        if self.buttons.meaning_mode:
            return events

        if not self._ocr_due():
            # Not an idle tick: the buttons were polled and acted on above. Only
            # the *page re-read* is skipped, which is the expensive half.
            self.frames_skipped += 1
            return events

        frame = self.camera.frame()
        if frame is not None:
            await self.runtime.feed_camera_frame(frame)
            self.frames_processed += 1

        return events

    def _ocr_due(self) -> bool:
        """Whether enough time has passed to re-read the page. Advances the schedule.

        Scheduled from the clock rather than by counting ticks, so the interval
        means the same thing whatever `tick_seconds` is, and so a tick that took
        longer than its budget does not push every later OCR pass out behind it.
        """

        if self.ocr_interval_seconds <= 0.0:
            return True

        now = self.clock.now()
        if self._next_ocr_at is None:
            # The first frame is read immediately. Waiting an interval would open
            # every session with a blind period in which the reader is looking at a
            # page the system has not seen.
            self._next_ocr_at = now + self.ocr_interval_seconds
            return True

        if now < self._next_ocr_at:
            return False

        # From `now`, not from the previous deadline: a session that fell behind
        # should resume its cadence, not fire a burst of catch-up OCR passes.
        self._next_ocr_at = now + self.ocr_interval_seconds
        return True

    async def run(self, *, max_ticks: int | None = None, **start: Any) -> Any:
        """Start the session, run until interrupted, finish it. Returns the analytics.

        The whole lifecycle, because the reference's `main_loop` was the whole
        lifecycle: it started the session, looped, and shut down in a `finally`.
        Splitting those would let a caller start ticking a session Reading Speed
        has never heard of, and the first pointer move would raise.

        `max_ticks` exists so a test or a timed demo can drive the real loop to
        completion instead of a copy of it. `start` is forwarded to
        `start_session` for the reader's voice and profile.
        """

        await self.runtime.engine.start_session(**start)

        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                await self.tick()
                ticks += 1
                await self.clock.sleep(self.tick_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("device loop interrupted; finishing session")
        finally:
            # Matches the reference's `finally: shutdown_session()`. An
            # interrupted session must still commit the page being read.
            analytics = await self.finish()

        return analytics

    async def _handle(self, event: SessionEvent) -> None:
        """Turn one hardware event into the runtime call it implies."""

        if event is SessionEvent.CAMERA_ON:
            self.runtime.engine.camera_on()
            return

        if event is SessionEvent.CAMERA_OFF:
            self.runtime.engine.camera_off("device stopped answering")
            return

        # Both buttons resolve a word by pointing; they differ in what happens
        # next, which is the engine's call and not the loop's.
        if event is SessionEvent.MEANING_MODE_ON:
            drain_results = await self._run_gesture(meaning=True)
            # Send any MeaningLookupResult to the OLED.
            for result in drain_results:
                if isinstance(result, MeaningLookupResult) and result.success:
                    await self._show_on_display(
                        f"{result.target_word.upper()}: {result.oled_text}"
                    )
            # The gesture publishes MEANING_REQUESTED only when it resolved a word
            # with confidence, so a missed fingertip would leave narration running
            # while the reader holds the switch — the one thing the switch is for.
            # Entering the mode without a word pauses playback and asks nothing,
            # which is the honest outcome: they stopped reading, we don't know at
            # what. Done after the gesture, never before: `meaning_mode_on` returns
            # early when already active, so pausing first would swallow the real
            # request and the explanation with it.
            #
            # Guard: only call the fallback if _run_gesture did NOT already trigger
            # meaning_mode_on via MEANING_REQUESTED (which would have set the flag).
            if not self.runtime.engine.state.is_meaning_mode:
                await self.runtime.engine.meaning_mode_on()
            return

        if event is SessionEvent.READING_UPDATE_REQUESTED:
            await self._run_gesture(meaning=False)
            return

        if event is SessionEvent.MEANING_MODE_OFF:
            # Called directly rather than through `handle_gesture_event`, which
            # has no branch for it — this edge comes from a switch, not a gesture.
            await self.runtime.engine.meaning_mode_off()

    async def _run_gesture(self, *, meaning: bool) -> list:
        """Capture a frame and resolve the fingertip to a word.

        The settle delay is the reference's, kept because it is about the reader
        and not about the code: a hand is still moving when the button is pressed.

        Returns the drain results list so the caller can extract
        ``MeaningLookupResult`` for the OLED.
        """

        if self.settle_seconds:
            await self.clock.sleep(self.settle_seconds)

        raw = self.camera.frame()
        if raw is None:
            logger.warning("gesture requested but the camera did not answer")
            return []

        image = self.camera.decode(raw)
        if image is None:
            logger.warning("gesture requested but the frame could not be decoded")
            return []

        _selection, drain_results = await self.runtime.feed_gesture_frame(
            image, meaning_gesture=meaning
        )
        self.gestures_run += 1
        return drain_results

    async def _show_on_display(self, text: str) -> None:
        """Send text to the OLED.  Swallows all errors.

        Display failures are non-fatal: a dead OLED must not stop reading.
        """

        if self.display is None:
            return
        try:
            await self.display.show(text)
        except Exception as error:
            logger.debug("display update failed: %s", error)

    async def finish(self) -> Any:
        """End the session: drain what is queued, then close it.

        Draining first is what keeps a final button press from being lost — the
        reference committed its memory on shutdown for the same reason.
        """

        await self.runtime.drain()
        return await self.runtime.engine.finish_session()
