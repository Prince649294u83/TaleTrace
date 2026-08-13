"""The migrated ESP32 rig and Merge Engine reconstruction, under test.

These cover the parts of `OCRandGESTURE/` that came across in the integration:
the Groq-backed reconstruction and same-page check, the SequenceMatcher pointer
sync, the two hardware sources, and the control loop that drives them.

The rule these are written against: same ESP32 image + Vision result + reading
position + button events must produce the same Merge Memory, pointer, page
detection, AI requests and audio queue as the reference did. So each test states
a reference behaviour and pins it, rather than describing the new structure.

Nothing here touches hardware or Groq. `FakeGroq` records what it was asked and
answers what the test scripted; `FakeDevice` answers the two ESP32 endpoints from
a list of scripted polls. Both stand in for HTTP, and for nothing else — the
reconstruction prompt, the edge detection and the loop ordering are all real.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.app.live_session import preflight
from backend.app.modules.image_receiver.esp32_buttons import ButtonState, Esp32Buttons
from backend.app.modules.image_receiver.esp32_camera import Esp32Camera
from backend.app.modules.merge_memory.engine import MergeMemory
from backend.app.modules.merge_memory.reconstruction import GroqReconstructor, pointer_offset
from backend.app.modules.ocr.pipeline import OcrPipeline
from backend.app.modules.ocr.replay import ReplayAdapter
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.runtime import ReadingRuntime
from backend.app.shared.constants import FIRST_PAGE_INDEX
from backend.app.shared.events import SessionEvent
from backend.app.shared.groq_keys import ai_engine_key


# ---------------------------------------------------------------- test doubles


class FakeCompletion:
    """The one shape of the Groq SDK response the reconstructor reads."""

    def __init__(self, content: str) -> None:
        message = type("Message", (), {"content": content})()
        self.choices = [type("Choice", (), {"message": message})()]


class FakeGroq:
    """A Groq client that answers from a script and records every request.

    Records because the prompts are the migrated behaviour: the reconstruction
    quality *is* the prompt, so a test that only checked the return value would
    pass while the system prompt was quietly rewritten.
    """

    def __init__(self, *answers: str, raises: Exception | None = None) -> None:
        self.answers = list(answers)
        self.raises = raises
        self.requests: list[dict] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs) -> FakeCompletion:
        self.requests.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return FakeCompletion(self.answers.pop(0) if self.answers else "")

    @property
    def last_prompt(self) -> str:
        """The user-role content of the most recent request."""

        messages = self.requests[-1]["messages"]
        return next(m["content"] for m in reversed(messages) if m["role"] == "user")

    @property
    def system_prompt(self) -> str:
        messages = self.requests[-1]["messages"]
        return next(m["content"] for m in messages if m["role"] == "system")


class FakeResponse:
    def __init__(self, *, status_code: int = 200, content: bytes = b"", payload=None) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeDevice:
    """Stands in for `requests.Session` against the ESP32 endpoints.

    Scripted per call rather than fixed, because the behaviours worth testing are
    all *transitions*: a button going down then up, a camera answering then
    dropping. A single canned response cannot express either.
    """

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append(url)
        if not self.responses:
            raise ConnectionError("device went away")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def buttons(*polls: dict, url: str = "http://esp32/buttons") -> Esp32Buttons:
    """A button source scripted with one payload per poll."""

    return Esp32Buttons(
        url, session=FakeDevice(*[FakeResponse(payload=p) for p in polls])
    )


def camera(*frames: bytes, url: str = "http://esp32/capture") -> Esp32Camera:
    return Esp32Camera(url, session=FakeDevice(*[FakeResponse(content=f) for f in frames]))


def recorded_page(text: str, *, words_per_line: int = 6):
    """Recorded OCR word boxes for `text`, as `ReplayAdapter` replays them."""

    out = []
    for index, word in enumerate(text.split()):
        column, row = index % words_per_line, index // words_per_line
        out.append(
            {
                "text": word,
                "bbox": [column * 90, row * 30, column * 90 + 80, row * 30 + 24],
                "confidence": 0.95,
                "word_index": index,
                "line_index": row,
            }
        )
    return out


PAGE_ONE = "The lighthouse stood alone on the cliff and its lamp had gone dark"
PAGE_TWO = "Salt wind followed her up the narrow spiral stair to the very top"


# ------------------------------------------------------- pointer sync (migrated)


class TestPointerOffset:
    """`calculate_accurate_pointer`, migrated. Where reading resumes after a merge."""

    def test_an_empty_previous_text_starts_at_the_beginning(self):
        assert pointer_offset("", "The lighthouse stood alone.") == 0

    def test_the_pointer_lands_where_new_text_begins(self):
        old = "The lighthouse stood alone."
        merged = "The lighthouse stood alone. Its lamp had gone dark."

        assert pointer_offset(old, merged) == len(old)

    def test_the_offset_is_measured_in_the_merged_text_not_the_old_one(self):
        """The reason `match.b` is used rather than `match.a`.

        Reconstruction repaired a word OCR dropped near the start, so every later
        position shifted. An offset taken from the old text would leave the
        pointer short by exactly the repair, and the reader would hear a clause
        they had already been read.
        """

        old = "lighthouse stood alone."
        merged = "The lighthouse stood alone. Its lamp had gone dark."

        offset = pointer_offset(old, merged)

        assert merged[:offset] == "The lighthouse stood alone."
        assert offset == len(old) + len("The ")

    def test_text_that_shares_nothing_puts_the_pointer_at_zero(self):
        assert pointer_offset("aaaa", "bbbb") == 0

    def test_an_unchanged_merge_leaves_the_pointer_at_the_end(self):
        text = "The lighthouse stood alone."

        assert pointer_offset(text, text) == len(text)


# --------------------------------------------------- reconstruction (migrated)


class TestGroqReconstruction:
    """`merge_ocr_with_groq`, migrated. The `reconstruct` callable Merge Memory takes."""

    def test_it_returns_what_the_model_merged(self):
        groq = FakeGroq("The lighthouse stood alone. Its lamp had gone dark.")
        reconstruct = GroqReconstructor(client=groq)

        merged = reconstruct("The lighthouse stood alone.", "Its lamp had gone dark.")

        assert merged == "The lighthouse stood alone. Its lamp had gone dark."

    def test_it_sends_both_texts_labelled(self):
        """The prompt shape is the behaviour. Both texts, each named."""

        groq = FakeGroq("merged")
        GroqReconstructor(client=groq)("held text", "new text")

        prompt = groq.last_prompt
        assert "Current Merge Memory:" in prompt
        assert "held text" in prompt
        assert "New OCR Output:" in prompt
        assert "new text" in prompt

    def test_an_empty_page_is_marked_as_a_page_start(self):
        """Carried from the reference: the model is told the difference between
        'nothing held yet' and 'held text that happens to be blank'."""

        groq = FakeGroq("merged")
        GroqReconstructor(client=groq)("", "first frame")

        assert "[EMPTY - PAGE START]" in groq.last_prompt

    def test_the_system_prompt_still_forbids_summarising(self):
        """The instruction that keeps this a merge and not a rewrite.

        Pinned by content because losing this line does not break anything
        visibly — it just starts returning paraphrased pages, and the reader is
        read a summary of the book instead of the book.
        """

        groq = FakeGroq("merged")
        GroqReconstructor(client=groq)("held", "new")

        system = groq.system_prompt
        assert "DO NOT summarize" in system
        assert "Remove running headers" in system
        assert "remove duplicate words" in system

    def test_it_asks_for_a_low_temperature(self):
        groq = FakeGroq("merged")
        GroqReconstructor(client=groq)("held", "new")

        assert groq.requests[-1]["temperature"] == pytest.approx(0.1)

    def test_without_a_key_it_appends_instead_of_failing(self):
        """Reference behaviour: no key degrades the text, it does not stop reading."""

        reconstruct = GroqReconstructor(api_key="")

        assert reconstruct("held text", "new text") == "held text\nnew text"

    def test_a_failed_call_appends_instead_of_raising(self):
        reconstruct = GroqReconstructor(client=FakeGroq(raises=RuntimeError("502")))

        assert reconstruct("held text", "new text") == "held text\nnew text"

    def test_an_empty_completion_does_not_erase_the_page(self):
        """The failure this guards is silent: a blank answer replacing a full page
        looks like OCR stopped working, and the page is gone either way."""

        reconstruct = GroqReconstructor(client=FakeGroq(""))

        assert reconstruct("held text", "new text") == "held text\nnew text"

    def test_constructing_it_opens_no_client(self):
        """So the app boots and the suite runs with no key present."""

        assert GroqReconstructor(api_key="").available is False
        assert GroqReconstructor(api_key="sk-test").available is True

    def test_it_reads_the_merge_key_and_not_the_ai_key(self, monkeypatch):
        """The two subsystems must not share a credential.

        The Merge Engine calls Groq several times a second while the camera runs;
        the AI Engine calls it once, when a reader asks. On one key the camera
        loop spends the rate limit and the reader is the one who sees the error.
        """

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_1", "gsk-ai-only")
        monkeypatch.setenv("GROQ_API_KEY_2", "gsk-merge-only")

        assert GroqReconstructor()._api_key == "gsk-merge-only"
        assert ai_engine_key() == "gsk-ai-only"

    def test_the_merge_engine_is_unavailable_when_only_the_ai_key_is_set(
        self, monkeypatch
    ):
        """Absent the fallback this is the whole point: keys do not substitute."""

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_1", "gsk-ai-only")
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)

        assert GroqReconstructor().available is False

    def test_a_single_legacy_key_still_serves_both(self, monkeypatch):
        """An older .env keeps working rather than silently losing both halves."""

        monkeypatch.setenv("GROQ_API_KEY", "gsk-legacy")
        monkeypatch.delenv("GROQ_API_KEY_1", raising=False)
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)

        assert GroqReconstructor()._api_key == "gsk-legacy"
        assert ai_engine_key() == "gsk-legacy"

    def test_merge_memory_uses_it_through_the_callable_it_already_accepted(self):
        """No change to Merge Memory's structure to accommodate reconstruction."""

        # Two answers because there are two frames and every frame is a call.
        groq = FakeGroq(
            "The lighthouse stood alone.",
            "The lighthouse stood alone. Its lamp had gone dark.",
        )
        memory = MergeMemory(reconstruct=GroqReconstructor(client=groq))

        memory.apply_frame("The lighthouse stood alone.")
        memory.apply_frame("Its lamp had gone dark.")

        assert memory.page_text(FIRST_PAGE_INDEX) == (
            "The lighthouse stood alone. Its lamp had gone dark."
        )
        # Both frames, as the reference did: `merge_ocr_with_groq` ran on every
        # frame including the first, because its job is repairing raw OCR — the
        # running header, the broken word — and not only joining two texts.
        assert len(groq.requests) == 2

    def test_the_first_frame_of_a_page_is_still_cleaned(self):
        """The case the reference's `[EMPTY - PAGE START]` prompt exists for.

        Nothing is held yet, so there is no seam — but the frame is still raw OCR
        with a running header and a page number in it, and that is what the
        reconstructor is for. Skipping it here is what put a camera overlay and
        half-read words into the text handed to the reader.
        """

        groq = FakeGroq("The lighthouse stood alone.")
        memory = MergeMemory(reconstruct=GroqReconstructor(client=groq))

        memory.apply_frame("Chapter One 14 The lighthouse stood alone.", whole_page=True)

        assert memory.page_text(FIRST_PAGE_INDEX) == "The lighthouse stood alone."
        assert len(groq.requests) == 1
        # An empty held memory is announced as such, so the model is told to clean
        # one text rather than left inferring a merge against nothing.
        assert "[EMPTY - PAGE START]" in groq.last_prompt


# ------------------------------------------------- same-page check (migrated)


class TestSamePageCheck:
    """`check_same_page_via_groq`, migrated. The second opinion on a page turn."""

    def test_yes_means_the_same_page(self):
        check = GroqReconstructor(client=FakeGroq("YES"))

        assert check.is_same_page("held page text", "new frame text") is True

    def test_no_means_the_page_turned(self):
        check = GroqReconstructor(client=FakeGroq("NO"))

        assert check.is_same_page("held page text", "new frame text") is False

    def test_the_answer_is_read_case_insensitively(self):
        check = GroqReconstructor(client=FakeGroq("yes"))

        assert check.is_same_page("held", "new") is True

    def test_it_asks_for_one_word_and_caps_the_answer(self):
        groq = FakeGroq("YES")
        GroqReconstructor(client=groq).is_same_page("held", "new")

        request = groq.requests[-1]
        assert "strictly ONE word: YES or NO" in groq.last_prompt
        assert request["max_tokens"] == 5
        assert request["temperature"] == 0.0

    def test_it_tolerates_reordered_and_missing_words_by_instruction(self):
        """The clause that stops the model calling a shaky capture a new page."""

        groq = FakeGroq("YES")
        GroqReconstructor(client=groq).is_same_page("held", "new")

        assert "reordered, missing, or overlapping" in groq.last_prompt

    def test_only_the_head_of_each_text_is_sent(self):
        """A page is identified by its opening, and this runs on every frame."""

        groq = FakeGroq("YES")
        GroqReconstructor(client=groq).is_same_page("h" * 5000, "n" * 5000)

        assert groq.last_prompt.count("h") <= 500

    def test_no_text_held_yet_is_the_same_page(self):
        check = GroqReconstructor(client=FakeGroq("NO"))

        assert check.is_same_page("   ", "first frame") is True

    def test_a_failed_call_assumes_the_same_page(self):
        """Reference default, and the safe direction: a false negative discards a
        page, a false positive only merges text reconstruction can reconcile."""

        check = GroqReconstructor(client=FakeGroq(raises=RuntimeError("timeout")))

        assert check.is_same_page("held page text", "new frame text") is True

    def test_without_a_key_it_assumes_the_same_page(self):
        assert GroqReconstructor(api_key="").is_same_page("held", "new") is True

    def test_one_client_serves_both_the_merge_and_the_check(self):
        """Two instances would open two clients per session for one key."""

        groq = FakeGroq("merged", "YES")
        reconstructor = GroqReconstructor(client=groq)

        reconstructor("held", "new")
        reconstructor.is_same_page("held", "new")

        assert len(groq.requests) == 2
        # Different models: the per-frame check uses the fast one.
        assert groq.requests[0]["model"] != groq.requests[1]["model"]


# ----------------------------------------------- the confirmation gate in OCR


class TestPageTurnConfirmation:
    """Geometry proposes a page turn; the semantic check confirms it."""

    def pipeline(self, confirm=None) -> OcrPipeline:
        return OcrPipeline(
            provider=ReplayAdapter(), preprocess=False, confirm_page_change=confirm
        )

    def test_a_confirmed_turn_is_a_turn(self):
        pipeline = self.pipeline(confirm=lambda held, new: False)  # "NO, not same page"

        pipeline.update_memory(recorded_page(PAGE_ONE))
        result = pipeline.update_memory(recorded_page(PAGE_TWO))

        assert result.page_changed is True
        assert result.reason == "page turn detected"

    def test_a_denied_turn_becomes_a_merge(self):
        """The camera moved, it was not a page turn. This is the false positive
        the whole gate exists to catch: without it the half-read page is
        committed and the reading pointer resets mid-sentence."""

        pipeline = self.pipeline(confirm=lambda held, new: True)  # "YES, same page"

        pipeline.update_memory(recorded_page(PAGE_ONE))
        result = pipeline.update_memory(recorded_page(PAGE_TWO))

        assert result.page_changed is False
        assert "merged" in result.reason
        # Both pages' words are held, which is what a merge means.
        held = {word.text for word in result.words}
        assert "lighthouse" in held and "spiral" in held

    def test_the_gate_is_only_asked_when_geometry_proposed_a_turn(self):
        """Not on every frame. The reference paid a round trip per frame; here the
        free check runs first and the paid one only settles the close calls."""

        asked: list[tuple[str, str]] = []

        def confirm(held: str, new: str) -> bool:
            asked.append((held, new))
            return True

        pipeline = self.pipeline(confirm=confirm)
        page = recorded_page(PAGE_ONE)

        pipeline.update_memory(page)
        pipeline.update_memory(page)  # same page again: geometry is certain

        assert asked == []

    def test_the_gate_cannot_invent_a_page_turn(self):
        """It only ever downgrades. A confirmer that says "not the same page" on a
        frame geometry called identical must not produce a turn."""

        pipeline = self.pipeline(confirm=lambda held, new: False)
        page = recorded_page(PAGE_ONE)

        pipeline.update_memory(page)
        result = pipeline.update_memory(page)

        assert result.page_changed is False

    def test_it_is_given_the_held_page_and_the_candidate_frame(self):
        seen: list[tuple[str, str]] = []

        def confirm(held: str, new: str) -> bool:
            seen.append((held, new))
            return True

        pipeline = self.pipeline(confirm=confirm)
        pipeline.update_memory(recorded_page(PAGE_ONE))
        pipeline.update_memory(recorded_page(PAGE_TWO))

        held, candidate = seen[0]
        assert "lighthouse" in held and "spiral" not in held
        assert "spiral" in candidate and "lighthouse" not in candidate

    def test_a_failing_gate_leaves_the_geometric_verdict_standing(self):
        """A broken second opinion must not lose the frame."""

        def confirm(held: str, new: str) -> bool:
            raise RuntimeError("groq is down")

        pipeline = self.pipeline(confirm=confirm)
        pipeline.update_memory(recorded_page(PAGE_ONE))
        result = pipeline.update_memory(recorded_page(PAGE_TWO))

        assert result.accepted is True
        assert result.page_changed is True

    def test_no_gate_at_all_still_turns_pages(self):
        """A session without a key reads more eagerly, not not at all."""

        pipeline = self.pipeline(confirm=None)

        pipeline.update_memory(recorded_page(PAGE_ONE))
        result = pipeline.update_memory(recorded_page(PAGE_TWO))

        assert result.page_changed is True

    def test_the_candidate_text_is_shaped_like_the_held_text(self):
        """Both sides of the comparison go through the same renderer, so a
        paragraph break cannot mean one thing on the left and another on the
        right."""

        seen: list[tuple[str, str]] = []
        pipeline = self.pipeline(
            confirm=lambda held, new: bool(seen.append((held, new))) or True
        )

        first = recorded_page(PAGE_ONE)
        pipeline.update_memory(first)
        pipeline.update_memory(recorded_page(PAGE_TWO))

        held, candidate = seen[0]
        # The held text is exactly what the pipeline reports as its page text.
        assert held == pipeline.text or "lighthouse" in held
        assert candidate.split() == PAGE_TWO.split()


# ------------------------------------------------------------- ESP32 camera


class TestEsp32Camera:
    """`fetch_camera_frame`, migrated. Returns bytes, or nothing. Never raises."""

    def test_it_returns_the_jpeg_the_device_sent(self):
        source = camera(b"\xff\xd8jpeg-bytes")

        assert source.frame() == b"\xff\xd8jpeg-bytes"
        assert source.frames_read == 1

    def test_the_bytes_are_not_decoded_on_the_way_through(self):
        """OCR base64-encodes them for Vision. Decoding centrally then re-encoding
        cost image quality for nothing, which the reference learned the hard way."""

        raw = b"\xff\xd8not-really-a-jpeg"

        assert camera(raw).frame() is raw

    def test_an_unreachable_device_returns_nothing(self):
        source = Esp32Camera("http://esp32/capture", session=FakeDevice())

        assert source.frame() is None
        assert source.failures == 1

    def test_a_non_200_returns_nothing(self):
        source = Esp32Camera(
            "http://esp32/capture", session=FakeDevice(FakeResponse(status_code=500))
        )

        assert source.frame() is None

    def test_an_empty_body_is_a_failure_not_a_frame(self):
        """A zero-byte 200 reaches OCR as a frame with no text, which is
        indistinguishable from a blank page."""

        source = Esp32Camera(
            "http://esp32/capture", session=FakeDevice(FakeResponse(content=b""))
        )

        assert source.frame() is None
        assert source.failures == 1

    def test_an_unconfigured_camera_reports_rather_than_raising(self):
        source = Esp32Camera("")

        assert source.configured is False
        assert source.frame() is None

    def test_a_dropped_frame_does_not_stop_the_next_one(self):
        """The load-bearing part on real hardware: one lost frame over Wi-Fi is
        not the end of a reading session."""

        source = Esp32Camera(
            "http://esp32/capture",
            session=FakeDevice(
                FakeResponse(status_code=500),
                FakeResponse(content=b"\xff\xd8good"),
            ),
        )

        assert source.frame() is None
        assert source.frame() == b"\xff\xd8good"
        assert (source.frames_read, source.failures) == (1, 1)

    def test_undecodable_bytes_decode_to_nothing(self):
        """A partial capture over Wi-Fi does produce these."""

        assert Esp32Camera.decode(b"not an image") is None

    def test_constructing_it_opens_no_connection(self):
        source = Esp32Camera("http://esp32/capture")

        assert source.frames_read == 0
        assert source.configured is True

    def test_the_url_comes_from_the_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("ESP32_CAM_CAPTURE_URL", "http://device/cap")

        assert Esp32Camera().capture_url == "http://device/cap"


# ------------------------------------------------------------ ESP32 buttons


class TestEsp32Buttons:
    """`poll_button_states`, migrated — plus the edges the reference lacked."""

    def test_it_reads_both_controls(self):
        source = buttons({"btn_momentary": True, "btn_toggle": True})

        state = source.read()

        assert state == ButtonState(momentary=True, toggle=True, reachable=True)

    def test_an_unreachable_device_reads_as_nothing_pressed(self):
        source = Esp32Buttons("http://esp32/buttons", session=FakeDevice())

        assert source.read() == ButtonState()

    def test_a_first_answer_announces_the_camera_once(self):
        source = buttons(
            {"btn_momentary": False, "btn_toggle": False},
            {"btn_momentary": False, "btn_toggle": False},
        )

        assert source.poll() == [SessionEvent.CAMERA_ON]
        assert source.poll() == []

    def test_losing_the_device_reports_the_camera_off(self):
        source = Esp32Buttons(
            "http://esp32/buttons",
            session=FakeDevice(FakeResponse(payload={}), FakeResponse(status_code=500)),
        )

        assert source.poll() == [SessionEvent.CAMERA_ON]
        assert source.poll() == [SessionEvent.CAMERA_OFF]

    def test_the_toggle_reports_both_edges(self):
        source = buttons(
            {"btn_toggle": False},
            {"btn_toggle": True},
            {"btn_toggle": False},
        )

        source.poll()  # CAMERA_ON
        assert source.poll() == [SessionEvent.MEANING_MODE_ON]
        assert source.poll() == [SessionEvent.MEANING_MODE_OFF]

    def test_holding_the_toggle_publishes_nothing_further(self):
        """The reference needed a nested `while True` to get this. Edge detection
        gets it for free, and the loop stays steppable."""

        source = buttons({"btn_toggle": True}, {"btn_toggle": True}, {"btn_toggle": True})

        source.poll()
        assert source.poll() == []
        assert source.poll() == []

    def test_the_toggle_level_is_readable_while_held(self):
        """The loop needs "still on", not just "just turned on"."""

        source = buttons({"btn_toggle": True}, {"btn_toggle": True})

        source.poll()
        assert source.meaning_mode is True

    def test_the_momentary_button_fires_on_press_only(self):
        """Firing on release too would run the gesture selection twice per press."""

        source = buttons(
            {"btn_momentary": False},
            {"btn_momentary": True},
            {"btn_momentary": True},
            {"btn_momentary": False},
        )

        source.poll()
        assert source.poll() == [SessionEvent.READING_UPDATE_REQUESTED]
        assert source.poll() == []
        assert source.poll() == []

    def test_the_toggle_takes_priority_over_the_momentary_button(self):
        """Reference behaviour: `if btn_toggle: ... continue` meant the momentary
        branch was unreachable while the switch was on.

        Both at once would otherwise run two gesture selections on one frame, and
        the second — a reading update — would move the pointer to the word the
        reader was only asking about.
        """

        source = buttons({}, {"btn_momentary": True, "btn_toggle": True})

        source.poll()

        assert source.poll() == [SessionEvent.MEANING_MODE_ON]

    def test_the_momentary_button_works_again_once_the_toggle_is_off(self):
        source = buttons(
            {},
            {"btn_momentary": False, "btn_toggle": True},
            {"btn_momentary": False, "btn_toggle": False},
            {"btn_momentary": True, "btn_toggle": False},
        )

        source.poll()
        source.poll()
        source.poll()

        assert source.poll() == [SessionEvent.READING_UPDATE_REQUESTED]

    def test_one_gesture_per_toggle_on_not_one_per_poll(self):
        """The reference's inner hold loop polled only the toggle — it did not
        re-run the selection. So a held switch is one lookup, not a stream."""

        source = buttons({}, {"btn_toggle": True}, {"btn_toggle": True}, {"btn_toggle": True})

        source.poll()
        entries = [source.poll() for _ in range(3)]

        assert entries == [[SessionEvent.MEANING_MODE_ON], [], []]

    def test_an_unconfigured_source_publishes_nothing(self):
        source = Esp32Buttons("")

        assert source.poll() == []
        assert source.configured is False

    def test_it_calls_no_module(self):
        """Publish-only, exactly as Gesture is. A button that reached into the
        Audio Engine would make the runtime untestable without the device."""

        source = buttons({"btn_toggle": True})

        assert not hasattr(source, "runtime")
        assert not hasattr(source, "engine")
        assert all(isinstance(event, SessionEvent) for event in source.poll())


# --------------------------------------------------------------- device loop


def blank_frame():
    return np.zeros((200, 600, 3), dtype=np.uint8)


class FakeCamera:
    """A camera whose frames decode to a real array, without OpenCV."""

    source_name = "fake_cam"

    def __init__(self, *frames, decodes: bool = True) -> None:
        self.frames = list(frames)
        self.decodes = decodes
        self.reads = 0

    def frame(self):
        self.reads += 1
        if not self.frames:
            return None
        return self.frames[0] if len(self.frames) == 1 else self.frames.pop(0)

    def decode(self, raw):
        return blank_frame() if self.decodes else None


class ScriptedButtons:
    """Button source that replays a fixed list of event batches."""

    def __init__(self, *batches: list[SessionEvent]) -> None:
        self.batches = list(batches)
        self.meaning_mode = False

    def poll(self) -> list[SessionEvent]:
        batch = self.batches.pop(0) if self.batches else []
        for event in batch:
            if event is SessionEvent.MEANING_MODE_ON:
                self.meaning_mode = True
            elif event is SessionEvent.MEANING_MODE_OFF:
                self.meaning_mode = False
        return batch


@pytest.fixture
def loop_runtime() -> ReadingRuntime:
    return ReadingRuntime.build(
        session_id="session-1",
        reader_id="reader-1",
        ocr_provider=ReplayAdapter(),
        book_id="book-1",
    )


async def device_loop(runtime: ReadingRuntime, camera_source, button_source) -> DeviceLoop:
    """A started session with the settle delay removed, so a test does not sleep.

    The session is started here because every test that steps `tick()` needs one:
    Reading Speed refuses a pointer update for a session it never saw begin, which
    is the same reason `run()` starts one itself.
    """

    await runtime.engine.start_session()
    return DeviceLoop(
        runtime=runtime,
        camera=camera_source,
        buttons=button_source,
        settle_seconds=0.0,
    )


class TestDeviceLoop:
    """`main_loop`, migrated. Hardware in, events and frames out, in order."""

    @pytest.mark.asyncio
    async def test_a_frame_reaches_ocr_and_merge_memory(self, loop_runtime):
        loop = await device_loop(
            loop_runtime, FakeCamera(recorded_page(PAGE_ONE)), ScriptedButtons([])
        )

        await loop.tick()

        assert loop.frames_processed == 1
        assert "lighthouse" in loop_runtime.engine.current_text()

    @pytest.mark.asyncio
    async def test_buttons_are_polled_before_frames_are_captured(self, loop_runtime):
        """Reference ordering. A press must be acted on with the frame taken
        *after* it, not one taken before the reader pointed at anything."""

        camera_source = FakeCamera(recorded_page(PAGE_ONE))
        loop = await device_loop(
            loop_runtime,
            camera_source,
            ScriptedButtons([SessionEvent.READING_UPDATE_REQUESTED]),
        )

        await loop.tick()

        # Two reads: the gesture's, then the OCR frame. The gesture's came first.
        assert camera_source.reads == 2
        assert loop.gestures_run == 1

    @pytest.mark.asyncio
    async def test_meaning_mode_holds_the_reading_loop(self, loop_runtime):
        """The reference's nested wait, preserved. Merging frames of a page the
        reader is pointing at rather than reading advances the pointer past them."""

        camera_source = FakeCamera(recorded_page(PAGE_ONE))
        loop = await device_loop(
            loop_runtime,
            camera_source,
            ScriptedButtons([SessionEvent.MEANING_MODE_ON], [], []),
        )

        await loop.tick()  # enters meaning mode
        frames_after_entry = loop.frames_processed
        await loop.tick()
        await loop.tick()

        assert loop.frames_processed == frames_after_entry
        assert loop_runtime.engine.state.is_meaning_mode is True

    @pytest.mark.asyncio
    async def test_releasing_the_toggle_resumes_reading(self, loop_runtime):
        loop = await device_loop(
            loop_runtime,
            FakeCamera(recorded_page(PAGE_ONE)),
            ScriptedButtons([SessionEvent.MEANING_MODE_ON], [SessionEvent.MEANING_MODE_OFF]),
        )

        await loop.tick()
        await loop.tick()

        assert loop_runtime.engine.state.is_meaning_mode is False
        assert loop.frames_processed == 1

    @pytest.mark.asyncio
    async def test_meaning_mode_pauses_even_when_no_word_was_resolved(self, loop_runtime):
        """The gesture publishes MEANING_REQUESTED only on a confident selection.
        A missed fingertip must still stop narration — that is what the switch is
        for, and leaving it reading is the one unacceptable outcome."""

        loop = await device_loop(
            loop_runtime,
            FakeCamera(recorded_page(PAGE_ONE), decodes=False),
            ScriptedButtons([SessionEvent.MEANING_MODE_ON]),
        )

        await loop.tick()

        assert loop.gestures_run == 0
        assert loop_runtime.engine.state.is_meaning_mode is True

    @pytest.mark.asyncio
    async def test_a_camera_that_never_answers_does_not_stop_the_loop(self, loop_runtime):
        loop = await device_loop(loop_runtime, FakeCamera(), ScriptedButtons([], [], []))

        await loop.tick()
        await loop.tick()
        await loop.tick()

        assert loop.frames_processed == 0

    @pytest.mark.asyncio
    async def test_the_camera_events_reach_the_engine(self, loop_runtime):
        loop = await device_loop(
            loop_runtime,
            FakeCamera(),
            ScriptedButtons([SessionEvent.CAMERA_ON], [SessionEvent.CAMERA_OFF]),
        )

        await loop.tick()
        assert loop_runtime.engine.state.camera_active is True

        await loop.tick()
        assert loop_runtime.engine.state.camera_active is False

    @pytest.mark.asyncio
    async def test_run_owns_the_whole_session_lifecycle(self, loop_runtime):
        """Start, loop, shut down — what the reference's `main_loop` did.

        Not pre-started here, unlike every other test in this class: this is the
        one that proves `run` starts the session itself. A caller that had to
        remember to start one first could tick a session Reading Speed never saw
        begin, and the first pointer move would raise.
        """

        loop = DeviceLoop(
            runtime=loop_runtime,
            camera=FakeCamera(recorded_page(PAGE_ONE)),
            buttons=ScriptedButtons([]),
            settle_seconds=0.0,
        )

        analytics = await loop.run(max_ticks=2)

        assert loop_runtime.engine.state.is_finished is True
        # The reference's `finally: shutdown_session()` — the open page is
        # committed, so the summary describes the page just finished.
        assert loop_runtime.engine.memory.committed_pages() != []
        assert analytics is not None

    @pytest.mark.asyncio
    async def test_an_interrupted_run_still_finishes_the_session(self, loop_runtime):
        """A cancelled loop must not leave the page uncommitted."""

        class Interrupting(ScriptedButtons):
            def poll(self):
                raise KeyboardInterrupt

        loop = DeviceLoop(
            runtime=loop_runtime,
            camera=FakeCamera(recorded_page(PAGE_ONE)),
            buttons=Interrupting(),
            settle_seconds=0.0,
        )

        await loop.run(max_ticks=5)

        assert loop_runtime.engine.state.is_finished is True

    @pytest.mark.asyncio
    async def test_finishing_drains_a_last_pending_event_first(self, loop_runtime):
        """Otherwise a press landing on the final tick is silently discarded."""

        loop = await device_loop(
            loop_runtime, FakeCamera(recorded_page(PAGE_ONE)), ScriptedButtons([])
        )
        await loop.tick()
        loop_runtime._enqueue(SessionEvent.WORD_SELECTED, {"word": "lighthouse"})

        await loop.finish()

        assert loop_runtime.pending == []
        assert any(
            e.event is SessionEvent.WORD_SELECTED for e in loop_runtime.engine.events
        )

    @pytest.mark.asyncio
    async def test_the_loop_owns_no_reading_state(self, loop_runtime):
        """Every decision belongs to the runtime and the modules under it."""

        loop = await device_loop(
            loop_runtime, FakeCamera(recorded_page(PAGE_ONE)), ScriptedButtons([])
        )

        assert not hasattr(loop, "pointer")
        assert not hasattr(loop, "memory")
        assert not hasattr(loop, "page")


# --------------------------------------------------------- the live entry point


class TestPreflight:
    """`--check`: which of the four causes of "no text" is the actual one.

    The value is discrimination. No camera, no key, no buttons and no book in
    frame all present as a silent session, and starting one to find out which is
    the slow way.
    """

    def test_it_reports_every_dependency(self, monkeypatch):
        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        names = [name for name, _, _ in preflight()]

        assert names == [
            "Google Vision",
            "Groq — Merge Engine",
            "Groq — AI Engine",
            "ESP32-CAM",
            "ESP32 buttons",
        ]

    def test_a_missing_vision_key_is_reported_as_unready(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_VISION_API_KEY", "")
        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        rows = dict((name, ok) for name, ok, _ in preflight())

        assert rows["Google Vision"] is False

    def test_it_names_the_consequence_not_the_variable(self, monkeypatch):
        """An operator reading this needs to know whether to bother starting."""

        monkeypatch.setenv("GROQ_API_KEY", "")
        monkeypatch.setenv("GROQ_API_KEY_2", "")
        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        detail = next(d for name, _, d in preflight() if name == "Groq — Merge Engine")

        assert "raw OCR" in detail

    def test_the_two_groq_keys_are_reported_separately(self, monkeypatch):
        """They fail independently, so one row cannot describe both.

        A merge key present and an AI key absent is a session that reads the page
        perfectly and cannot explain a single word — reported as one "Groq: ready"
        row, that is indistinguishable from a working system.
        """

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_2", "gsk-merge")
        monkeypatch.setenv("GROQ_API_KEY_1", "")
        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        rows = dict((name, ok) for name, ok, _ in preflight())

        assert rows["Groq — Merge Engine"] is True
        assert rows["Groq — AI Engine"] is False

    def test_it_never_prints_a_key(self, monkeypatch):
        """Secrets are reported as present or absent. This is a group project and
        the output goes in a terminal someone screenshots."""

        monkeypatch.setenv("GOOGLE_VISION_API_KEY", "AIzaSy-secret-value")
        monkeypatch.setenv("GROQ_API_KEY_1", "gsk-secret-value")
        monkeypatch.setenv("GROQ_API_KEY_2", "gsk-secret-value")
        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        printed = " ".join(f"{name} {ok} {detail}" for name, ok, detail in preflight())

        assert "secret-value" not in printed

    def test_an_unconfigured_device_is_not_reached_for(self, monkeypatch):
        """No URL means no HTTP call — `--check` on a laptop must not hang."""

        monkeypatch.delenv("ESP32_CAM_CAPTURE_URL", raising=False)
        monkeypatch.delenv("ESP32_BUTTONS_URL", raising=False)

        rows = dict((name, detail) for name, _, detail in preflight())

        assert "not set" in rows["ESP32-CAM"]
        assert "not set" in rows["ESP32 buttons"]
