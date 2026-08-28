"""Monotonic sequence counters and structured hardware telemetry logging."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Process-wide monotonic counter for raw hardware events
_hardware_seq_lock = threading.Lock()
_global_hardware_seq = 0


def next_hardware_sequence() -> int:
    """Increment and return the process-wide monotonic hardware sequence ID."""
    global _global_hardware_seq
    with _hardware_seq_lock:
        _global_hardware_seq += 1
        return _global_hardware_seq


@dataclass
class SessionTelemetryTracker:
    """Tracks session-scoped monotonic sequence numbers and event histories."""

    session_id: str
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_sequence(self) -> int:
        """Increment and return the next session sequence number."""
        with self._lock:
            self._seq += 1
            return self._seq

    @property
    def current_sequence(self) -> int:
        """Return the current sequence number."""
        with self._lock:
            return self._seq

    def log_event(self, tag: str, details: str = "", metadata: dict[str, Any] | None = None) -> int:
        """Log a structured telemetry entry with current sequence and hardware sequence."""
        seq = self.next_sequence()
        hw_seq = next_hardware_sequence()
        meta_str = f" meta={metadata}" if metadata else ""
        detail_str = f" {details}" if details else ""
        logger.info("[SEQ:%d] [HW_SEQ:%d] [%s]%s%s", seq, hw_seq, tag, detail_str, meta_str)
        return seq
