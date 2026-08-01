"""The scripted event timeline.

Each step is one event the real system will eventually produce — a Reading
Update from gaze tracking, Meaning Mode from a tap, an OCR frame from the
camera, a Page Turn from gesture detection. Here they are simply scheduled, so
the whole reading session is reproducible and reviewable.

Separated from `simulate_reading.py` so the sequence can be changed without
touching the runner, and so the runner can be pointed at a different timeline.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from backend.demo import console
from backend.demo.fake_reading_engine import FakeReadingEngine


@dataclass
class Step:
    """One scheduled event.

    `delay` is how long to wait before firing, in simulated seconds. `action`
    receives the reading engine and returns a short description for the console.
    """

    delay: float
    label: str
    action: Callable[[FakeReadingEngine], Awaitable[str]]


async def _reading_update(reading: FakeReadingEngine) -> str:
    await reading.move_pointer(sentence_index=2)
    return "reader's eyes moved to sentence 2 — jump lands after the current sentence"


async def _meaning_on(reading: FakeReadingEngine) -> str:
    await reading.enable_meaning_mode()
    return "word tapped — speech must stop immediately, reading clock freezes"


async def _meaning_off(reading: FakeReadingEngine) -> str:
    await reading.disable_meaning_mode()
    return "explanation delivered — resumes the interrupted sentence, not the next one"


async def _ocr_update(reading: FakeReadingEngine) -> str:
    version, applied = await reading.ocr_update()
    verb = "applied" if applied else "rejected"
    return f"merge memory refined to v{version} — {verb}; current sentence undisturbed"


async def _stale_ocr(reading: FakeReadingEngine) -> str:
    applied = await reading.stale_ocr_update(version=1)
    return (
        "out-of-order frame (v1) delivered late — "
        f"{'WRONGLY APPLIED' if applied else 'correctly rejected'}"
    )


async def _next_paragraph(reading: FakeReadingEngine) -> str:
    result = await reading.next_paragraph()
    return "advanced to the next paragraph" if result else "no further paragraph on this page"


async def _page_turn(reading: FakeReadingEngine) -> str:
    result = await reading.turn_page()
    return "page turned — previous page committed, queue rebuilt" if result else "no next page"


# The rehearsal. Delays are simulated seconds; --realtime makes them literal.
TIMELINE: list[Step] = [
    Step(6, "Reading Update", _reading_update),
    Step(5, "Meaning Mode ENABLED", _meaning_on),
    Step(4, "Meaning Mode DISABLED", _meaning_off),
    Step(5, "Merge Memory Updated", _ocr_update),
    Step(3, "Stale OCR Frame", _stale_ocr),
    Step(5, "Paragraph Advance", _next_paragraph),
    Step(6, "Page Turn", _page_turn),
]


async def run_timeline(
    reading: FakeReadingEngine,
    *,
    sleep: Callable[[float], Awaitable[None]],
    show_state: Callable[[], None],
) -> None:
    """Fire each step in order, printing what happened and the resulting state."""

    for step in TIMELINE:
        await sleep(step.delay)
        detail = await step.action(reading)
        console.event(step.label, detail)
        show_state()
