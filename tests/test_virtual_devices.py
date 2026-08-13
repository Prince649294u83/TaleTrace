"""The virtual device layer, under test.

Two claims are worth pinning here, and they are different in kind.

The first is *equivalence*: a virtual camera and a virtual button source must be
indistinguishable to `DeviceLoop` from the ESP32 rig. Not similar — indistinguishable,
because the entire value of a simulated session is that it is evidence about the
real one. So these tests assert against the same protocols the hardware satisfies,
and several of them run the same assertion against both implementations.

The second is *cost*: the loop must not re-read the page ten times a second. That
one is a deliberate departure from `OCRandGESTURE/`, whose `main_loop` called
`process_frame` — a Vision call plus two Groq calls — on every 0.1s pass. It is
tested here rather than in `test_device_integration.py` because the fix lives in
the loop's cadence, and the virtual clock is what makes the cadence observable
without waiting real seconds for it.

Nothing here touches hardware, a network, or a key.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.app.modules.image_receiver import (
    ButtonSource,
    CameraSource,
    Esp32Buttons,
    Esp32Camera,
    VirtualButtons,
    VirtualCamera,
)
from backend.app.modules.image_receiver.virtual_buttons import ScriptedButtons
from backend.app.modules.ocr.replay import ReplayAdapter
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.shared.clock import RealClock, VirtualClock
from backend.app.shared.events import SessionEvent

# A minimal JPEG the decoder will accept, built once. Real bytes rather than a
# placeholder because `VirtualCamera.decode` runs the production OpenCV path, and
# a fake would prove the decode was skipped rather than that it worked.
try:
    import cv2

    _OK, _BUFFER = cv2.imencode(".jpg", np.full((40, 120, 3), 200, dtype=np.uint8))
    JPEG = _BUFFER.tobytes() if _OK else b""
except ImportError:  # pragma: no cover - OpenCV is a hard dependency in practice
    JPEG = b""


@pytest.fixture
def images(tmp_path):
    """Three distinct one-page images on disk."""

    paths = []
    for index in range(3):
        path = tmp_path / f"page_{index:03d}.jpg"
        path.write_bytes(JPEG)
        paths.append(path)
    return paths


# ------------------------------------------------------------------- contracts


class TestTheContractsMatch:
    """Both families satisfy both protocols. This is the whole premise."""

    def test_the_hardware_camera_satisfies_the_camera_protocol(self):
        assert isinstance(Esp32Camera(), CameraSource)

    def test_the_virtual_camera_satisfies_the_same_protocol(self):
        assert isinstance(VirtualCamera([JPEG]), CameraSource)

    def test_the_hardware_buttons_satisfy_the_button_protocol(self):
        assert isinstance(Esp32Buttons(), ButtonSource)

    def test_the_virtual_buttons_satisfy_the_same_protocol(self):
        assert isinstance(VirtualButtons(), ButtonSource)

    def test_meaning_mode_is_a_bool_on_both_and_not_a_method(self):
        """The bug this pins: a method here is always truthy.

        `DeviceLoop` reads `buttons.meaning_mode` to decide whether to skip frame
        capture. An implementation exposing it as a method rather than a property
        would be read as permanently true, the loop would never capture another
        frame, and the session would silently stop reading while every test that
        only checked events kept passing.
        """

        for source in (Esp32Buttons(), VirtualButtons(), ScriptedButtons([])):
            assert isinstance(source.meaning_mode, bool), type(source).__name__


# ---------------------------------------------------------------------- camera


class TestVirtualCamera:
    def test_a_single_image_is_returned_on_every_frame(self, images):
        camera = VirtualCamera.from_image(images[0])

        assert camera.frame() == JPEG
        assert camera.frame() == JPEG
        assert camera.frames_read == 2

    def test_a_sequence_hands_over_one_image_per_frame_in_order(self, images):
        camera = VirtualCamera.from_images(images)

        assert [camera.frame(), camera.frame(), camera.frame()] == [JPEG, JPEG, JPEG]
        assert camera.current_path == images[2]

    def test_an_exhausted_sequence_reports_none_rather_than_raising(self, images):
        """The hardware's answer for "no frame right now", so the loop just retries."""

        camera = VirtualCamera.from_images(images)
        for _ in images:
            camera.frame()

        assert camera.frame() is None
        assert camera.exhausted

    def test_a_directory_is_read_in_name_order(self, tmp_path):
        """Sorted, because filesystem order is not reproducible across machines.

        A hundred-image run that fails on image 34 has to fail on the same image
        next time to be worth anything.
        """

        for name in ("c.jpg", "a.jpg", "b.jpg"):
            (tmp_path / name).write_bytes(JPEG)

        camera = VirtualCamera.from_directory(tmp_path)

        assert [p.name for p in camera._paths] == ["a.jpg", "b.jpg", "c.jpg"]

    def test_a_missing_file_fails_at_construction_not_mid_session(self, tmp_path):
        """A typo should cost nothing, not sixty images' worth of API calls."""

        with pytest.raises(FileNotFoundError):
            VirtualCamera.from_images([tmp_path / "nope.jpg"])

    def test_the_frame_decodes_through_the_production_opencv_path(self, images):
        camera = VirtualCamera.from_image(images[0])

        decoded = camera.decode(camera.frame())

        assert decoded is not None
        assert decoded.shape == (40, 120, 3)

    def test_undecodable_bytes_report_none_as_a_partial_capture_would(self):
        assert VirtualCamera([b"not a jpeg"]).decode(b"not a jpeg") is None


class TestTimedStream:
    """The interval decides when the page turns, not whether the camera answers."""

    def test_the_camera_always_answers_even_between_page_turns(self, images):
        """The bug this pins, and it made gestures fail in simulation only.

        A real ESP32-CAM responds to every capture; what changes slowly is the page
        in front of it. An earlier version returned None between scheduled frames,
        so a gesture capture — which asks "what is in front of me right now" — got
        nothing, and `_run_gesture` logged "the camera did not answer" on a rig that
        was working perfectly. A virtual device that is less available than the
        hardware exercises paths the product never reaches.
        """

        clock = VirtualClock()
        camera = VirtualCamera.streaming(images, interval_seconds=10.0, clock=clock)

        assert camera.frame() is not None
        clock.advance(0.1)
        assert camera.frame() is not None
        clock.advance(0.1)
        assert camera.frame() is not None

    def test_the_page_advances_only_when_the_interval_has_passed(self, images):
        clock = VirtualClock()
        camera = VirtualCamera.streaming(images, interval_seconds=10.0, clock=clock)

        camera.frame()
        assert camera.current_path == images[0]

        clock.advance(5.0)
        camera.frame()
        assert camera.current_path == images[0], "turned early"

        clock.advance(5.0)
        camera.frame()
        assert camera.current_path == images[1]

    def test_the_last_page_is_held_rather_than_running_out(self, images):
        """A session outlasting its images has the reader still holding a book.

        An exhausted camera would leave the tail of a long run measuring an idle
        system and reporting it as stability.
        """

        clock = VirtualClock()
        camera = VirtualCamera.streaming(images, interval_seconds=1.0, clock=clock)

        camera.frame()  # starts the stream's timeline, as the loop's first tick does
        clock.advance(500.0)

        assert camera.frame() is not None
        assert camera.current_path == images[-1]
        assert not camera.exhausted

    def test_the_page_is_derived_from_elapsed_time_not_from_the_number_of_reads(
        self, images
    ):
        """The bug this pins: stepping one page per call.

        The page in front of the camera must depend on how much time has passed, not
        on how often the camera happened to be polled. Stepping per call meant a loop
        that skipped ahead — a stress run, or a tick that ran long — landed on an
        earlier page than the clock said, so every later assertion about which page
        was being read was quietly wrong.
        """

        clock = VirtualClock()
        camera = VirtualCamera.streaming(images, interval_seconds=10.0, clock=clock)

        camera.frame()
        clock.advance(20.0)
        camera.frame()

        assert camera.current_index == 2, "one read should not mean one page"


# --------------------------------------------------------------------- buttons


class TestVirtualButtons:
    def test_the_camera_is_announced_once_as_a_live_rig_does(self):
        buttons = VirtualButtons()

        assert buttons.poll() == [SessionEvent.CAMERA_ON]
        assert buttons.poll() == []

    def test_each_control_produces_the_event_the_hardware_produces(self):
        buttons = VirtualButtons(camera_on=False)

        buttons.reading_update()
        buttons.set_meaning_mode(True)
        buttons.set_meaning_mode(False)
        buttons.audio_off()
        buttons.audio_on()
        buttons.camera_on()
        buttons.camera_off()

        assert buttons.poll() == [
            SessionEvent.READING_UPDATE_REQUESTED,
            SessionEvent.MEANING_MODE_ON,
            SessionEvent.MEANING_MODE_OFF,
            SessionEvent.SESSION_PAUSED,
            SessionEvent.SESSION_RESUMED,
            SessionEvent.CAMERA_ON,
            SessionEvent.CAMERA_OFF,
        ]

    def test_the_toggle_reports_edges_not_levels(self):
        """Held on, it must fire once. `Esp32Buttons` honours this and so must this."""

        buttons = VirtualButtons(camera_on=False)

        buttons.set_meaning_mode(True)
        assert buttons.poll() == [SessionEvent.MEANING_MODE_ON]

        buttons.set_meaning_mode(True)
        buttons.set_meaning_mode(True)
        assert buttons.poll() == [], "re-fired while merely still held"

    def test_the_momentary_button_is_suppressed_while_the_toggle_is_held(self):
        """The reference's `if btn_toggle: ... continue`, preserved.

        Without it, a reader holding the switch and pressing the button runs two
        gesture selections on one frame, and the second moves the pointer to a word
        they were only asking about.
        """

        buttons = VirtualButtons(camera_on=False)
        buttons.set_meaning_mode(True)
        buttons.poll()

        buttons.reading_update()

        assert buttons.poll() == []

    def test_two_presses_between_polls_both_survive(self):
        """Rapid presses are specifically what the reliability tests reproduce."""

        buttons = VirtualButtons(camera_on=False)

        buttons.reading_update()
        buttons.reading_update()

        assert buttons.poll() == [SessionEvent.READING_UPDATE_REQUESTED] * 2


class TestScriptedButtons:
    def test_presses_fire_at_their_scheduled_session_time(self):
        clock = VirtualClock()
        buttons = ScriptedButtons(
            [(2.0, "reading_update"), (5.0, "meaning_on")], clock=clock, camera_on=False
        )

        assert buttons.poll() == []
        clock.advance(2.0)
        assert buttons.poll() == [SessionEvent.READING_UPDATE_REQUESTED]
        clock.advance(3.0)
        assert buttons.poll() == [SessionEvent.MEANING_MODE_ON]
        assert buttons.finished

    def test_a_poll_that_skips_past_several_fires_all_of_them_in_order(self):
        """Coalescing would quietly turn a script into a different script."""

        clock = VirtualClock()
        buttons = ScriptedButtons(
            [(1.0, "reading_update"), (2.0, "meaning_on"), (3.0, "meaning_off")],
            clock=clock,
            camera_on=False,
        )

        buttons.poll()  # starts the script's clock, as the loop's first tick does
        clock.advance(10.0)

        assert buttons.poll() == [
            SessionEvent.READING_UPDATE_REQUESTED,
            SessionEvent.MEANING_MODE_ON,
            SessionEvent.MEANING_MODE_OFF,
        ]

    def test_time_starts_at_the_first_poll_not_at_construction(self):
        """Setup time must not eat into the script.

        A session is built before it is started — credentials resolved, images read
        from disk — and anchoring to construction would fire the opening presses
        before the loop had ticked once.
        """

        clock = VirtualClock()
        buttons = ScriptedButtons([(1.0, "reading_update")], clock=clock, camera_on=False)

        clock.advance(30.0)  # a slow build

        assert buttons.poll() == [], "fired during setup"
        clock.advance(1.0)
        assert buttons.poll() == [SessionEvent.READING_UPDATE_REQUESTED]

    def test_an_unknown_action_is_refused_at_construction(self):
        """A typo must not read as a press that silently never happens.

        A simulation that skipped the press it was written to test would still pass,
        which is the worst way for this to fail.
        """

        with pytest.raises(ValueError, match="unknown button action"):
            ScriptedButtons([(1.0, "meaning_mode")])

    def test_the_schedule_is_sorted_so_source_order_need_not_be_time_order(self):
        clock = VirtualClock()
        buttons = ScriptedButtons(
            [(5.0, "meaning_off"), (1.0, "meaning_on")], clock=clock, camera_on=False
        )

        buttons.poll()  # starts the script's clock
        clock.advance(1.0)
        assert buttons.poll() == [SessionEvent.MEANING_MODE_ON]


# ----------------------------------------------------------------------- clock


class TestVirtualClock:
    def test_sleeping_advances_session_time_by_the_full_amount(self):
        """The subtle one, and the reason this class exists.

        A clock that returned real time while sleeping zero would report a
        fifteen-minute session as having taken no time, and every metric derived
        from elapsed time — WPM, Reading Difficulty, Idle Time — would be plausible
        garbage.
        """

        import asyncio

        clock = VirtualClock(speed=0.0)
        asyncio.run(clock.sleep(900.0))

        assert clock.now() == 900.0
        assert clock.slept_seconds == 900.0

    def test_the_real_wait_is_scaled_but_session_time_is_not(self):
        import asyncio
        import time

        clock = VirtualClock(speed=0.0)
        started = time.monotonic()
        asyncio.run(clock.sleep(60.0))
        real = time.monotonic() - started

        assert clock.now() == 60.0
        assert real < 1.0, "waited real time for a zero-speed clock"

    def test_time_never_runs_backwards(self):
        clock = VirtualClock()

        with pytest.raises(ValueError):
            clock.advance(-1.0)

    def test_the_real_clock_satisfies_the_same_protocol(self):
        from backend.app.shared.clock import Clock

        assert isinstance(RealClock(), Clock)
        assert isinstance(VirtualClock(), Clock)


# ------------------------------------------------------------------- the loop


def loop_runtime() -> ReadingRuntime:
    return ReadingRuntime.build(
        session_id="virtual-session",
        reader_id="virtual-reader",
        ocr_provider=ReplayAdapter(),
        book_id="book-1",
    )


class RecordingCamera:
    """Counts captures. Frames are word fixtures the replay adapter accepts."""

    source_name = "recording"

    def __init__(self, words) -> None:
        self.words = words
        self.captures = 0

    def frame(self):
        self.captures += 1
        return self.words

    def decode(self, raw):
        return np.zeros((40, 120, 3), dtype=np.uint8)


def word_fixture(text: str, *, count: int = 40):
    """Enough words for OCR to accept the frame as a page."""

    return [
        {
            "text": f"{text}{index}",
            "bbox": [index * 10, 0, index * 10 + 8, 12],
            "confidence": 0.99,
            "word_index": index,
            "line_index": index // 8,
            "paragraph_index": 0,
        }
        for index in range(count)
    ]


class TestTheLoopDoesNotReReadThePageEveryTick:
    """The cost fix, pinned.

    `OCRandGESTURE/main_loop` slept 0.1s and called `process_frame` on every pass.
    `process_frame` is one Google Vision call plus two Groq calls, so a minute of
    reading was ~600 Vision and ~1200 Groq calls for a page that changes when a
    human turns it. The buttons still poll every tick; only the page re-read is
    throttled.
    """

    @pytest.mark.asyncio
    async def test_ocr_runs_once_per_interval_not_once_per_tick(self):
        clock = VirtualClock(speed=0.0)
        camera = RecordingCamera(word_fixture("alpha"))
        runtime = loop_runtime()
        await runtime.engine.start_session()

        loop = DeviceLoop(
            runtime=runtime,
            camera=camera,
            buttons=VirtualButtons(),
            clock=clock,
            settle_seconds=0.0,
            tick_seconds=0.1,
            ocr_interval_seconds=1.0,
        )

        await loop.run(max_ticks=100)

        # 100 ticks over 10 session seconds: the first frame plus one a second.
        assert loop.frames_processed == pytest.approx(11, abs=1)
        assert loop.frames_skipped >= 85

    @pytest.mark.asyncio
    async def test_the_buttons_are_still_polled_on_every_tick(self):
        """Responsiveness is the half of the reference's cadence that was right."""

        clock = VirtualClock(speed=0.0)
        buttons = VirtualButtons()
        runtime = loop_runtime()
        await runtime.engine.start_session()

        loop = DeviceLoop(
            runtime=runtime,
            camera=RecordingCamera(word_fixture("alpha")),
            buttons=buttons,
            clock=clock,
            settle_seconds=0.0,
            ocr_interval_seconds=1.0,
        )

        await loop.run(max_ticks=50)

        assert buttons.polls >= 50

    @pytest.mark.asyncio
    async def test_the_first_frame_is_read_immediately(self):
        """No blind period at the start: the reader is already looking at a page."""

        clock = VirtualClock(speed=0.0)
        runtime = loop_runtime()
        await runtime.engine.start_session()

        loop = DeviceLoop(
            runtime=runtime,
            camera=RecordingCamera(word_fixture("alpha")),
            buttons=VirtualButtons(),
            clock=clock,
            settle_seconds=0.0,
            ocr_interval_seconds=1.0,
        )

        await loop.tick()

        assert loop.frames_processed == 1

    @pytest.mark.asyncio
    async def test_a_zero_interval_restores_the_reference_behaviour(self):
        """The differential harness needs to compare like with like."""

        clock = VirtualClock(speed=0.0)
        runtime = loop_runtime()
        await runtime.engine.start_session()

        loop = DeviceLoop(
            runtime=runtime,
            camera=RecordingCamera(word_fixture("alpha")),
            buttons=VirtualButtons(),
            clock=clock,
            settle_seconds=0.0,
            ocr_interval_seconds=0.0,
        )

        await loop.run(max_ticks=20)

        assert loop.frames_processed == 20
        assert loop.frames_skipped == 0


class TestASessionRunsOnVirtualHardware:
    """End to end, no rig: the claim the whole layer exists to support."""

    @pytest.mark.asyncio
    async def test_a_scripted_session_reads_pages_and_runs_gestures(self):
        clock = VirtualClock(speed=0.0)
        runtime = loop_runtime()
        buttons = ScriptedButtons(
            [(1.0, "reading_update"), (3.0, "meaning_on"), (5.0, "meaning_off")],
            clock=clock,
        )

        loop = DeviceLoop(
            runtime=runtime,
            camera=RecordingCamera(word_fixture("alpha")),
            buttons=buttons,
            clock=clock,
            settle_seconds=0.0,
            ocr_interval_seconds=1.0,
        )

        analytics = await loop.run(max_ticks=80)

        assert loop.frames_processed > 0
        assert loop.gestures_run == 2, "one per reading update, one entering Meaning Mode"
        assert SessionEvent.MEANING_MODE_ON in loop.events_published
        assert SessionEvent.MEANING_MODE_OFF in loop.events_published
        assert analytics is not None

    @pytest.mark.asyncio
    async def test_the_page_is_not_re_read_while_meaning_mode_is_held(self):
        """The reference's nested hold-loop, preserved without the nested loop.

        The reader has stopped reading and is asking about a word; merging frames of
        a page they are pointing at would advance the pointer past where they are.
        """

        clock = VirtualClock(speed=0.0)
        runtime = loop_runtime()
        camera = RecordingCamera(word_fixture("alpha"))
        buttons = ScriptedButtons([(0.5, "meaning_on")], clock=clock)

        loop = DeviceLoop(
            runtime=runtime,
            camera=camera,
            buttons=buttons,
            clock=clock,
            settle_seconds=0.0,
            ocr_interval_seconds=1.0,
        )
        await runtime.engine.start_session()

        for _ in range(5):
            await loop.tick()
        held = loop.frames_processed

        for _ in range(40):
            await loop.tick()

        assert loop.frames_processed == held, "kept reading while the switch was held"
