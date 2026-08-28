"""Observation fusion and detector selection hysteresis for 3-tier gesture engine."""

from __future__ import annotations

import math
from typing import Sequence

from backend.app.modules.gesture_engine.selection_models import DetectionObservation, FingerObservation


class ObservationFuser:
    """Fuses multi-tier detector observations (MediaPipe, Partial-Finger CV, Tracker).

    Criteria for confidence boost:
      1. Normalized spatial distance between detectors < agreement_distance_ratio.
      2. Angular direction difference < 25 degrees (0.436 radians).
    """

    def __init__(
        self,
        agreement_distance_ratio: float = 0.035,  # ~35px on a 1024x768 frame
        agreement_angle_deg: float = 25.0,
        hysteresis_decay: float = 0.8,
    ) -> None:
        self.agreement_distance_ratio = agreement_distance_ratio
        self.agreement_angle_deg = agreement_angle_deg
        self.hysteresis_decay = hysteresis_decay
        self._preferred_detector = "mediapipe"
        self._consecutive_preferred_hits = 0

    def fuse(
        self,
        observations: Sequence[DetectionObservation],
        image_dims: tuple[int, int] = (1024, 768),
        frame_id: int = 0,
        page_id: str = "page_default",
    ) -> FingerObservation | None:
        """Fuse available observations into a single FingerObservation with provenance."""
        valid_obs = [obs for obs in observations if obs is not None and obs.confidence > 0.0]
        if not valid_obs:
            self._consecutive_preferred_hits = max(0, self._consecutive_preferred_hits - 1)
            return None

        # Map observations by detector name
        by_detector = {obs.detector_name: obs for obs in valid_obs}
        mp_obs = by_detector.get("mediapipe")
        partial_obs = by_detector.get("partial_finger") or by_detector.get("contour_fallback")
        tracker_obs = by_detector.get("tracker")

        width, height = image_dims
        diag = math.hypot(width, height) if width > 0 and height > 0 else 1000.0

        # Case 1: Both MediaPipe and Partial-Finger CV are present
        if mp_obs is not None and partial_obs is not None:
            dist = math.hypot(mp_obs.x - partial_obs.x, mp_obs.y - partial_obs.y)
            norm_dist = dist / diag

            spatial_agree = norm_dist < self.agreement_distance_ratio

            angular_agree = False
            if mp_obs.direction is not None and partial_obs.direction is not None:
                dot = (
                    mp_obs.direction[0] * partial_obs.direction[0]
                    + mp_obs.direction[1] * partial_obs.direction[1]
                )
                dot = max(-1.0, min(1.0, dot))
                angle_deg = math.degrees(math.acos(dot))
                angular_agree = angle_deg < self.agreement_angle_deg
            elif mp_obs.direction is None and partial_obs.direction is None:
                angular_agree = True

            if spatial_agree and angular_agree:
                # Strong fusion boost
                boosted_conf = min(1.0, max(mp_obs.confidence, partial_obs.confidence) + 0.15)
                # Weighted midpoint for coordinate accuracy
                fused_x = 0.6 * mp_obs.x + 0.4 * partial_obs.x
                fused_y = 0.6 * mp_obs.y + 0.4 * partial_obs.y
                fused_dir = mp_obs.direction or partial_obs.direction

                self._consecutive_preferred_hits += 1
                return FingerObservation(
                    frame_id=frame_id,
                    page_id=page_id,
                    x=fused_x,
                    y=fused_y,
                    direction=fused_dir,
                    confidence=boosted_conf,
                    geometry_score=max(mp_obs.geometry_score, partial_obs.geometry_score),
                    primary_source="mediapipe",
                    supporting_sources=("partial_finger",),
                    provenance="fused",
                    is_predicted=False,
                )

        # Case 2: Apply detector selection hysteresis
        # Prefer higher-confidence observation while giving slight inertia to consecutive hits
        primary_obs: DetectionObservation
        if mp_obs is not None and partial_obs is not None:
            # Hysteresis adjustment
            mp_score = mp_obs.confidence + (0.05 if self._preferred_detector == "mediapipe" else 0.0)
            part_score = partial_obs.confidence + (0.05 if self._preferred_detector == "partial_finger" else 0.0)
            if mp_score >= part_score:
                primary_obs = mp_obs
                self._preferred_detector = "mediapipe"
            else:
                primary_obs = partial_obs
                self._preferred_detector = "partial_finger"
        elif mp_obs is not None:
            primary_obs = mp_obs
            self._preferred_detector = "mediapipe"
        elif partial_obs is not None:
            primary_obs = partial_obs
            self._preferred_detector = "partial_finger"
        elif tracker_obs is not None:
            primary_obs = tracker_obs
        else:
            return None

        # Build final observation
        supporting: list[str] = []
        if tracker_obs is not None and tracker_obs.detector_name != primary_obs.detector_name:
            supporting.append("tracker")

        return FingerObservation(
            frame_id=frame_id,
            page_id=page_id,
            x=primary_obs.x,
            y=primary_obs.y,
            direction=primary_obs.direction,
            confidence=primary_obs.confidence,
            geometry_score=primary_obs.geometry_score,
            primary_source=primary_obs.detector_name,
            supporting_sources=tuple(supporting),
            provenance=primary_obs.detector_name,
            is_predicted=primary_obs.is_predicted,
        )
