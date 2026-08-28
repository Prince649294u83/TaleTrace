"""Tier 3: Lightweight Temporal Motion Tracker for gesture stabilization."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from backend.app.modules.gesture_engine.selection_models import DetectionObservation

logger = logging.getLogger(__name__)

# Configurable decay factors for missed frames
TRACK_CONFIDENCE_DECAY_1 = 0.80
TRACK_CONFIDENCE_DECAY_2 = 0.55
MAX_COAST_FRAMES = 2


@dataclass
class TrackerState:
    """Internal state vector for fingertip motion tracking."""

    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    dir_x: float = 0.0
    dir_y: float = -1.0
    confidence: float = 0.0
    missed_frames: int = 0
    last_timestamp: float = 0.0
    is_active: bool = False


class GestureMotionTracker:
    """Tracks fingertip position and velocity over time to smooth jitter and bridge brief detection gaps.

    Safety invariant: Predicted observations are marked with is_predicted=True and decaying
    confidence. They can stabilize existing tracks but can never independently generate a new
    high-confidence selection after prolonged loss.
    """

    def __init__(
        self,
        decay_1: float = TRACK_CONFIDENCE_DECAY_1,
        decay_2: float = TRACK_CONFIDENCE_DECAY_2,
        max_coast: int = MAX_COAST_FRAMES,
    ) -> None:
        self.decay_1 = decay_1
        self.decay_2 = decay_2
        self.max_coast = max_coast
        self.state = TrackerState()

    def reset(self) -> None:
        """Reset tracking state."""
        self.state = TrackerState()

    def update(
        self,
        observation: DetectionObservation | None,
        timestamp: float | None = None,
        frame_id: int = 0,
    ) -> DetectionObservation | None:
        """Update tracker with new observation or coast forward if missing."""
        t = timestamp if timestamp is not None else time.time()

        # Strong observation from MediaPipe or Partial-Finger CV
        if observation is not None and observation.confidence >= 0.35 and not observation.is_predicted:
            dt = t - self.state.last_timestamp if self.state.last_timestamp > 0 else 0.1
            dt = max(0.01, min(0.5, dt))

            if self.state.is_active and self.state.missed_frames == 0:
                # Alpha-beta filter update
                measured_vx = (observation.x - self.state.x) / dt
                measured_vy = (observation.y - self.state.y) / dt
                # Clamp unrealistic velocities
                max_speed = 3000.0  # px/sec
                measured_speed = math.hypot(measured_vx, measured_vy)
                if measured_speed > max_speed:
                    scale = max_speed / measured_speed
                    measured_vx *= scale
                    measured_vy *= scale

                self.state.vx = 0.6 * self.state.vx + 0.4 * measured_vx
                self.state.vy = 0.6 * self.state.vy + 0.4 * measured_vy
                self.state.x = 0.7 * observation.x + 0.3 * (self.state.x + self.state.vx * dt)
                self.state.y = 0.7 * observation.y + 0.3 * (self.state.y + self.state.vy * dt)
            else:
                self.state.x = observation.x
                self.state.y = observation.y
                self.state.vx = 0.0
                self.state.vy = 0.0

            if observation.direction is not None:
                self.state.dir_x = observation.direction[0]
                self.state.dir_y = observation.direction[1]

            self.state.confidence = observation.confidence
            self.state.missed_frames = 0
            self.state.last_timestamp = t
            self.state.is_active = True

            return DetectionObservation(
                detector_name="tracker",
                x=self.state.x,
                y=self.state.y,
                direction=(self.state.dir_x, self.state.dir_y) if (self.state.dir_x != 0 or self.state.dir_y != 0) else None,
                confidence=self.state.confidence,
                geometry_score=0.9,
                direction_confidence=0.9,
                is_predicted=False,
                timestamp=t,
                frame_id=frame_id,
            )

        # Coasting forward when detection is missing
        if self.state.is_active:
            self.state.missed_frames += 1
            if self.state.missed_frames > self.max_coast:
                self.reset()
                return None

            dt = t - self.state.last_timestamp if self.state.last_timestamp > 0 else 0.1
            dt = max(0.01, min(0.5, dt))

            # Coast with damped velocity
            self.state.x += self.state.vx * dt * 0.5
            self.state.y += self.state.vy * dt * 0.5
            self.state.vx *= 0.5
            self.state.vy *= 0.5

            decay = self.decay_1 if self.state.missed_frames == 1 else self.decay_2
            self.state.confidence *= decay
            self.state.last_timestamp = t

            return DetectionObservation(
                detector_name="tracker",
                x=self.state.x,
                y=self.state.y,
                direction=(self.state.dir_x, self.state.dir_y) if (self.state.dir_x != 0 or self.state.dir_y != 0) else None,
                confidence=self.state.confidence,
                geometry_score=0.5,
                direction_confidence=0.5,
                is_predicted=True,
                timestamp=t,
                frame_id=frame_id,
            )

        return None
