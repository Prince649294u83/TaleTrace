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
("the toggle just turned on"). Comparing against the previous poll turns the
level back into the two edges that carry meaning.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from backend.app.modules.image_receiver.types import DisplayState, HealthTelemetry
from backend.app.shared.events import SessionEvent

logger = logging.getLogger(__name__)

# Short: this is polled inside the control loop, and a slow answer delays OCR.
_POLL_TIMEOUT = 0.3


@dataclass(frozen=True)
class ButtonState:
    """One poll of the device's two controls and dual-state combination."""

    momentary: bool = False
    toggle: bool = False
    both_active: bool = False
    reachable: bool = False


class Esp32Buttons:
    """Polls the ESP32 button endpoint and reports state changes as events.

    Holds the previous poll so it can report edges. One instance per session.

    Also implements ``DisplayTarget``: the same ESP32 serves the OLED at
    ``/display``, and wiring display alongside buttons keeps a single HTTP
    session to one device.
    """

    source_name = "esp32_buttons"

    def __init__(
        self,
        buttons_url: str | None = None,
        *,
        timeout: float = _POLL_TIMEOUT,
        session: Any = None,
        display_url: str | None = None,
    ) -> None:
        self.buttons_url = (
            buttons_url if buttons_url is not None else os.environ.get("ESP32_BUTTONS_URL", "")
        )
        self.display_url = (
            display_url if display_url is not None else os.environ.get("ESP32_DISPLAY_URL", "")
        )
        self.timeout = timeout
        self._session = session
        self._previous = ButtonState()
        self._camera_announced = False
        self._initialized = False
        self._display_state = DisplayState.IDLE

    @property
    def configured(self) -> bool:
        return bool(self.buttons_url)

    @property
    def display_configured(self) -> bool:
        """Whether a display endpoint was provided."""
        return bool(self.display_url)

    @property
    def display_state(self) -> DisplayState:
        return self._display_state

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
        than as an error.
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

        momentary = bool(data.get("btn_momentary", False))
        toggle = bool(data.get("btn_toggle", False))
        both = bool(data.get("both_active", False))

        return ButtonState(
            momentary=momentary,
            toggle=toggle,
            both_active=both,
            reachable=True,
        )

    def check_health(self) -> HealthTelemetry:
        """Probe the device GET /health endpoint with fallback for legacy firmware."""
        if not self.configured:
            return HealthTelemetry(
                device="button_oled",
                protocol_version=0,
                firmware_version="unknown",
                ip="",
                port=8080,
                uptime_ms=0,
                wifi_rssi=0,
                oled=False,
                is_legacy=True,
            )

        health_url = self.buttons_url.rsplit("/", 1)[0] + "/health"
        try:
            response = self._http().get(health_url, timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                return HealthTelemetry(
                    device=str(data.get("device", "button_oled")),
                    protocol_version=int(data.get("protocol_version", 1)),
                    firmware_version=str(data.get("firmware_version", "1.0")),
                    ip=str(data.get("ip", "")),
                    port=int(data.get("port", 8080)),
                    uptime_ms=int(data.get("uptime_ms", 0)),
                    wifi_rssi=int(data.get("wifi_rssi", 0)),
                    oled=bool(data.get("oled", True)),
                    is_legacy=False,
                )
        except Exception as error:
            logger.debug("Health check probe failed: %s", error)

        # Legacy firmware fallback: device answered on /buttons, health is legacy
        read_state = self.read()
        return HealthTelemetry(
            device="button_oled",
            protocol_version=0,
            firmware_version="legacy",
            ip="",
            port=8080,
            uptime_ms=0,
            wifi_rssi=0,
            oled=read_state.reachable,
            is_legacy=True,
        )

    def poll(self) -> list[SessionEvent]:
        """Poll the device and return the events its state change implies."""
        current = self.read()
        events: list[SessionEvent] = []
        previous = self._previous

        # Announce device presence on first successful poll
        if current.reachable and not self._camera_announced:
            events.append(SessionEvent.CAMERA_ON)
            self._camera_announced = True
        elif not current.reachable and self._camera_announced:
            events.append(SessionEvent.CAMERA_OFF)
            self._camera_announced = False

        # The latching switch: report both edges
        if current.toggle and not previous.toggle:
            events.append(SessionEvent.MEANING_MODE_ON)
        elif previous.toggle and not current.toggle:
            events.append(SessionEvent.MEANING_MODE_OFF)

        # The momentary button: suppressed while toggle is on
        if current.momentary and not previous.momentary and not current.toggle:
            events.append(SessionEvent.READING_UPDATE_REQUESTED)

        self._previous = current
        return events

    # -------------------------------------------------------------------- OLED

    @staticmethod
    def _normalize_oled_text(
        text: str,
        max_length: int | None = None,
        max_lines: int = 40,
        chars_per_line: int = 18,
    ) -> str:
        """Normalize text for the 128x64 SH1106 OLED matching firmware wrapping.

        Replaces curly quotes/dashes with standard ASCII, collapses whitespace,
        and limits total text to the firmware's buffer.
        """
        replacements = {
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
            "\u2014": "-",
            "\u2013": "-",
            "\u2022": "*",
            "\u2026": "...",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)

        cleaned = " ".join(text.split())
        effective_limit = max_length if max_length is not None else (max_lines * chars_per_line)
        if len(cleaned) > effective_limit:
            return cleaned[: effective_limit - 3] + "..."
        return cleaned

    async def show(self, text: str, display_id: str | None = None) -> None:
        """Send text to the ESP32 OLED display. Never raises.

        Wire format: ``POST /display`` with ``Content-Type: text/plain`` and
        the text as the raw body. Optionally passes ``X-TaleTrace-Display-Id``.
        """
        if not self.display_configured:
            return

        display_text = self._normalize_oled_text(text)
        if not display_text:
            return

        self._display_state = DisplayState.SENDING
        headers = {"Content-Type": "text/plain"}
        if display_id:
            headers["X-TaleTrace-Display-Id"] = display_id

        try:
            start_t = time.time()
            response = self._http().post(
                self.display_url,
                data=display_text,
                headers=headers,
                timeout=2.0,
            )
            latency_ms = (time.time() - start_t) * 1000.0
            if response.status_code == 200:
                self._display_state = DisplayState.ACKNOWLEDGED
                logger.debug("OLED update succeeded in %.1fms", latency_ms)
            else:
                self._display_state = DisplayState.FAILED
                logger.warning(
                    "OLED display returned %d in %.1fms: %s",
                    response.status_code,
                    latency_ms,
                    response.text[:100],
                )
        except Exception as error:
            self._display_state = DisplayState.FAILED
            logger.debug("OLED display unreachable: %s", error)


