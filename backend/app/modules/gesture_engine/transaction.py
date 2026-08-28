"""GestureTransaction container and lifecycle management."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from backend.app.modules.gesture_engine.selection_models import (
    FingerObservation,
    FrameContext,
    SelectionResult,
)
from backend.app.shared.events import SessionEvent


class TransactionStatus(str, Enum):
    """Lifecycle status for a physical gesture interaction."""

    PENDING = "PENDING"
    CAPTURING = "CAPTURING"
    SELECTING = "SELECTING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class GestureTransaction:
    """Bounded, auditable container for one physical gesture interaction.

    Enforces lifecycle deadlines, cancellation propagation across async tasks,
    and stale-transaction guards before any side effects are published.
    """

    transaction_id: str
    session_id: str
    page_id: str
    started_at: float
    deadline_monotonic: float
    status: TransactionStatus = TransactionStatus.PENDING
    cancellation_event: asyncio.Event = field(default_factory=asyncio.Event)
    button_event: SessionEvent = SessionEvent.READING_UPDATE_REQUESTED
    frames: list[FrameContext] = field(default_factory=list)
    observations: list[FingerObservation] = field(default_factory=list)
    candidate_selections: list[SelectionResult] = field(default_factory=list)
    final_result: SelectionResult | None = None
    completion_reason: str = ""

    def is_valid(self, active_session_id: str, active_page_id: str) -> bool:
        """Check whether the transaction is still current, active, and unexpired."""
        return (
            not self.cancellation_event.is_set()
            and self.session_id == active_session_id
            and self.page_id == active_page_id
            and time.monotonic() <= self.deadline_monotonic
            and self.status not in (TransactionStatus.CANCELLED, TransactionStatus.EXPIRED)
        )

    def cancel(self, reason: str = "cancelled") -> None:
        """Cancel this transaction and propagate cancellation event."""
        self.cancellation_event.set()
        self.status = TransactionStatus.CANCELLED
        self.completion_reason = reason

    def complete(self, result: SelectionResult, reason: str = "success") -> None:
        """Complete this transaction with final result."""
        self.final_result = result
        self.status = TransactionStatus.COMPLETED
        self.completion_reason = reason

    def reject(self, reason: str = "NO_STABLE_SELECTION") -> None:
        """Mark as completed with safe rejection."""
        self.status = TransactionStatus.COMPLETED
        self.completion_reason = reason
