"""HardwareSupervisor: Decoupled independent health and connection supervisor for TaleTrace hardware."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable

from backend.app.modules.image_receiver.types import DeviceHealthState, DisplayState, HealthTelemetry
from backend.app.shared.events import SessionEvent

logger = logging.getLogger(__name__)


class SupervisorPingInterval(float, Enum):
    """Adaptive poll intervals in seconds."""

    NOMINAL = 1.0
    ACTIVE_BURST = 0.05
    ERROR_BACKOFF = 3.0


class HardwareSupervisor:
    """Monitors camera, buttons, and OLED display reachability independently."""

    def __init__(
        self,
        camera_client: Any = None,
        buttons_client: Any = None,
        event_callback: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        self.camera = camera_client
        self.buttons = buttons_client
        self.event_callback = event_callback

        self.camera_state = DeviceHealthState.DISCONNECTED
        self.buttons_state = DeviceHealthState.DISCONNECTED
        self.display_state = DisplayState.IDLE

        self.camera_telemetry = HealthTelemetry(
            device="camera",
            protocol_version=0,
            firmware_version="unknown",
            ip="",
            port=80,
            uptime_ms=0,
            wifi_rssi=0,
            oled=False,
            is_legacy=True,
        )
        self.buttons_telemetry = HealthTelemetry(
            device="buttons",
            protocol_version=0,
            firmware_version="unknown",
            ip="",
            port=8080,
            uptime_ms=0,
            wifi_rssi=0,
            oled=False,
            is_legacy=True,
        )

        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_hardware_ready(self) -> bool:
        """Startup gate: Camera READY and Buttons READY are required to begin reading."""
        return (
            self.camera_state == DeviceHealthState.READY
            and self.buttons_state == DeviceHealthState.READY
        )

    def update_camera_status(self, is_reachable: bool) -> None:
        """Update camera reachability state."""
        prev = self.camera_state
        if is_reachable:
            self.camera_state = DeviceHealthState.READY
            if prev in (DeviceHealthState.DISCONNECTED, DeviceHealthState.DEGRADED):
                if self.event_callback:
                    self.event_callback(SessionEvent.CAMERA_ON)
        else:
            self.camera_state = DeviceHealthState.DISCONNECTED
            if prev == DeviceHealthState.READY:
                if self.event_callback:
                    self.event_callback(SessionEvent.CAMERA_OFF)

    def update_buttons_status(self, is_reachable: bool) -> None:
        """Update buttons reachability state."""
        prev = self.buttons_state
        if is_reachable:
            self.buttons_state = DeviceHealthState.READY
            if prev in (DeviceHealthState.DISCONNECTED, DeviceHealthState.DEGRADED):
                if self.event_callback:
                    self.event_callback(SessionEvent.BUTTON_DEVICE_ONLINE)
        else:
            self.buttons_state = DeviceHealthState.DISCONNECTED
            if prev == DeviceHealthState.READY:
                if self.event_callback:
                    self.event_callback(SessionEvent.BUTTON_DEVICE_OFFLINE)

    def probe_all_sync(self) -> dict[str, Any]:
        """Perform a synchronous health probe across all devices."""
        cam_ok = False
        if self.camera is not None and getattr(self.camera, "configured", False):
            try:
                frame = self.camera.frame()
                cam_ok = frame is not None
            except Exception:
                cam_ok = False
        self.update_camera_status(cam_ok)

        btn_ok = False
        if self.buttons is not None and getattr(self.buttons, "configured", False):
            try:
                health = self.buttons.check_health()
                self.buttons_telemetry = health
                btn_ok = health.oled or (not health.is_legacy)
                self.display_state = DisplayState.READY if health.oled else DisplayState.UNREACHABLE
            except Exception:
                btn_ok = False
                self.display_state = DisplayState.UNREACHABLE
        self.update_buttons_status(btn_ok)

        return {
            "camera_state": self.camera_state.value,
            "buttons_state": self.buttons_state.value,
            "display_state": self.display_state.value,
            "is_ready": self.is_hardware_ready,
        }
