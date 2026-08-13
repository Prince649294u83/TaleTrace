"""The ESP32 hardware buttons as an event producer.

Migrated from `poll_button_states` and the button branches of `main_loop` in
`OCRandGESTURE/main_controller.py`. The device exposes two controls:

    btn_momentary   pressed and released  -> re-read from where I am pointing
    btn_toggle      latching, held on/off -> Meaning Mode

The reference reacted to those inline: it called the gesture engine, printed the
result, and for the toggle sat in a `while True` polling loop until the switch was
physically turned off. That is why it could not be tested without the hardware.

Here the source does one thing — turn polled pin states into events — and
publishes them:

    ESP32 buttons -> poll() -> [SessionEvent, ...] -> runtime -> modules

It calls no module, exactly as Gesture does not. The runtime decides that
MEANING_MODE_ON means "stop the reading clock and run a lookup"; a button has no
business knowing that.

Edges, not levels
-----------------
The wire reports a *level* ("the toggle is on"), but the system needs an *edge*
("the toggle just turned on"). Publishing on level would republish MEANING_MODE_ON
on every tick for as long as the reader held the switch, and the reference's
`while True` hold-loop existed precisely to work around that. Comparing against
the previous poll turns the level back into the two edges that carry meaning, and
the hold behaviour falls out for free: no further event fires until the switch
actually moves.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from backend.app.shared.events import SessionEvent

logger = logging.getLogger(__name__)

# Short: this is polled inside the control loop, and a slow answer delays OCR.
# The reference used 0.3s for the same reason.
_POLL_TIMEOUT = 0.3


@dataclass(frozen=True)
class ButtonState:
    """One poll of the device's two controls."""

    momentary: bool = False
    toggle: bool = False
    reachable: bool = False


class Esp32Buttons:
    """Polls the ESP32 button endpoint and reports state changes as events.

    Holds the previous poll so it can report edges. One instance per session.
    """

    source_name = "esp32_buttons"

    def __init__(
        self,
        buttons_url: str | None = None,
        *,
        timeout: float = _POLL_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.buttons_url = (
            buttons_url if buttons_url is not None else os.environ.get("ESP32_BUTTONS_URL", "")
        )
        self.timeout = timeout
        self._session = session
        self._previous = ButtonState()
        self._camera_announced = False

    @property
    def configured(self) -> bool:
        return bool(self.buttons_url)

    @property
    def meaning_mode(self) -> bool:
        """Whether the latching switch is currently held on.

        The runtime reads this to know Meaning Mode is *still* active without
        having to remember the last event it saw.
        """

        return self._previous.toggle

    def _http(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def read(self) -> ButtonState:
        """One poll of the device. Never raises.

        An unreachable device reads as "nothing pressed, not reachable" rather
        than as an error, matching the reference: a dropped poll must not stop the
        reading loop.
        """

        if not self.configured:
            return ButtonState()

        try:
            response = self._http().get(self.buttons_url, timeout=self.timeout)
            if response.status_code != 200:
                return ButtonState()
            data = response.json()
        except Exception as error:
            logger.debug("ESP32 button poll failed: %s", error)
            return ButtonState()

        return ButtonState(
            momentary=bool(data.get("btn_momentary", False)),
            toggle=bool(data.get("btn_toggle", False)),
            reachable=True,
        )

    def poll(self) -> list[SessionEvent]:
        """Poll the device and return the events its state change implies.

        Returns them in the order the runtime should apply them. Nothing is
        published when nothing changed, which is what makes this safe to call on
        every tick of the control loop.
        """

        current = self.read()
        events: list[SessionEvent] = []
        previous = self._previous

        # The device answering at all means the rig is powered and streaming.
        # Announced once rather than on every poll.
        if current.reachable and not self._camera_announced:
            events.append(SessionEvent.CAMERA_ON)
            self._camera_announced = True
        elif not current.reachable and self._camera_announced:
            events.append(SessionEvent.CAMERA_OFF)
            self._camera_announced = False

        # The latching switch: report both edges so the runtime can stop the
        # reading clock on entry and restart it on exit.
        if current.toggle and not previous.toggle:
            events.append(SessionEvent.MEANING_MODE_ON)
        elif previous.toggle and not current.toggle:
            events.append(SessionEvent.MEANING_MODE_OFF)

        # The momentary button: only the press is meaningful. Firing on release
        # too would run the gesture selection twice per press.
        #
        # Suppressed while the toggle is on, because the reference's `if btn_toggle:
        # ... continue` meant the momentary branch was never reached in that state.
        # Without this a reader holding the switch and pressing the button runs two
        # gesture selections on one frame, and the second one — a reading update —
        # moves the pointer to the word they were only asking about.
        if current.momentary and not previous.momentary and not current.toggle:
            events.append(SessionEvent.READING_UPDATE_REQUESTED)

        self._previous = current
        return events
