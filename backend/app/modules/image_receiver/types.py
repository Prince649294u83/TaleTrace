"""Hardware protocol types, state machines, and timeout classifications."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HardwareTimeoutType(str, Enum):
    """Granular classification of hardware and transport failure modes."""

    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    READ_TIMEOUT = "READ_TIMEOUT"
    BAD_RESPONSE = "BAD_RESPONSE"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    STALE_DATA = "STALE_DATA"


class DisplayState(str, Enum):
    """Lifecycle state machine for the OLED display peripheral."""

    IDLE = "IDLE"
    SENDING = "SENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"


class DeviceHealthState(str, Enum):
    """4-state lifecycle for independent hardware peripherals."""

    DISCONNECTED = "DISCONNECTED"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class HealthTelemetry:
    """Parsed self-describing diagnostic telemetry from GET /health."""

    device: str
    protocol_version: int
    firmware_version: str
    ip: str
    port: int
    uptime_ms: int
    wifi_rssi: int
    oled: bool
    is_legacy: bool = False
