"""Weighted temporal consensus and hysteresis for multi-frame gesture bursts."""

from __future__ import annotations

import collections
import logging
from typing import Sequence

from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
)

logger = logging.getLogger(__name__)


class GestureConsensus:
    """Evaluates candidate word selections across burst frames to produce a rock-solid stable selection."""

    def __init__(
        self,
        config: SelectionConfig | None = None,
        min_support_ratio: float = 0.5,  # At least 3 out of 5 frames
        min_winner_confidence: float = 0.45,
    ) -> None:
        self.config = config or SelectionConfig()
        self.min_support_ratio = min_support_ratio
        self.min_winner_confidence = min_winner_confidence

    def resolve(
        self,
        results: Sequence[SelectionResult],
        active_word_index: int | None = None,
    ) -> SelectionResult:
        """Resolve a sequence of per-frame SelectionResults into a consensus result."""
        valid_results = [r for r in results if r is not None and r.succeeded and r.selected_word]
        if not valid_results:
            # No successful frames in the burst
            sample_finger = next((r.finger_point for r in results if r and r.finger_point), None)
            return SelectionResult(
                status=SelectionStatus.LOW_CONFIDENCE,
                finger_point=sample_finger,
                selection_reason="No valid word selection across burst frames (NO_STABLE_SELECTION)",
            )

        # Count occurrences and aggregate weighted confidence
        # Key by (word_index, selected_word)
        counts: dict[tuple[int, str], int] = collections.defaultdict(int)
        confidence_sums: dict[tuple[int, str], float] = collections.defaultdict(float)
        exemplars: dict[tuple[int, str], SelectionResult] = {}

        for r in valid_results:
            key = (r.word_index, r.selected_word)
            counts[key] += 1
            confidence_sums[key] += r.confidence
            exemplars[key] = r

        # Sort candidates by vote count then aggregated confidence
        sorted_candidates = sorted(
            counts.keys(),
            key=lambda k: (
                counts[k],
                confidence_sums[k] / counts[k],
                # Hysteresis: slight bonus to active word if applicable
                0.15 if (active_word_index is not None and k[0] == active_word_index) else 0.0,
            ),
            reverse=True,
        )

        top_key = sorted_candidates[0]
        top_votes = counts[top_key]
        top_mean_conf = confidence_sums[top_key] / top_votes
        top_result = exemplars[top_key]

        total_frames = len(results)
        support_ratio = top_votes / max(total_frames, 1)

        # Check minimum support ratio and confidence
        if support_ratio < self.min_support_ratio or top_mean_conf < self.min_winner_confidence:
            return SelectionResult(
                status=SelectionStatus.LOW_CONFIDENCE,
                finger_point=top_result.finger_point,
                candidate_scores=top_result.candidate_scores,
                selection_reason=(
                    f"Consensus support ({top_votes}/{total_frames} = {support_ratio:.2f}) "
                    f"or confidence ({top_mean_conf:.2f}) below threshold"
                ),
                detector=top_result.detector,
            )

        # If runner-up exists, check margin
        if len(sorted_candidates) > 1:
            second_key = sorted_candidates[1]
            second_votes = counts[second_key]
            second_mean_conf = confidence_sums[second_key] / second_votes
            if top_votes == second_votes:
                # Tie broken by mean confidence margin
                conf_margin = top_mean_conf - second_mean_conf
                if conf_margin < self.config.selection_margin:
                    return SelectionResult(
                        status=SelectionStatus.LOW_CONFIDENCE,
                        finger_point=top_result.finger_point,
                        candidate_scores=top_result.candidate_scores,
                        selection_reason=f"Tie between '{top_key[1]}' and '{second_key[1]}' without sufficient margin",
                        detector=top_result.detector,
                    )

        evidence = top_result.evidence
        if evidence is not None:
            second_votes = counts[sorted_candidates[1]] if len(sorted_candidates) > 1 else 0
            second_mean_conf = (confidence_sums[sorted_candidates[1]] / second_votes) if second_votes > 0 else 0.0
            evidence = evidence.model_copy(
                update={
                    "temporal_stability_frames": top_votes,
                    "runner_up_temporal_support": second_votes,
                    "runner_up_peak_score": second_mean_conf,
                }
            )
            top_result = top_result.model_copy(update={"evidence": evidence})

        return top_result
