"""Debug overlay for a selection result. Diagnostics only — nothing depends on it.

Migrated from `OCRandGESTURE/Gesture/gesture_engine/visualizer.py` with the three
modes intact (`basic` fingertip + selected word, `developer` adds the search zone
and target line, `debug` adds candidate scores and the pointing vector).

Two fixes carried over from the standalone version:

  - it imported `SelectionConfig` from `word_selector`, which only worked because
    that module happened to re-export it. The import is direct now.
  - `np.mean` over a list of ints is replaced with plain division. It was building
    an array per frame to average five numbers.

This module is the one place in the gesture pipeline allowed to be optional: if
OpenCV is missing, `draw_overlay` returns the frame untouched instead of raising.
A missing overlay is a missing debug aid; a raise here would take down a reading
session for the sake of a rectangle.
"""

from __future__ import annotations

from typing import Any

from backend.app.modules.gesture_engine.selection_models import (
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
)

_GREEN = (0, 255, 0)
_RED = (0, 0, 255)
_WHITE = (255, 255, 255)
_YELLOW = (0, 255, 255)
_CYAN = (255, 255, 0)
_BLUE = (255, 0, 0)
_MAGENTA = (255, 0, 255)
_GREY = (200, 200, 200)


def draw_overlay(image: Any, result: SelectionResult, mode: str = "basic") -> Any:
    """Render `result` onto a copy of `image`. The input frame is never mutated."""

    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover
        return image

    canvas = image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX

    if result.finger_point is None:
        # No fingertip means there is nothing to anchor an overlay to, so the
        # status alone is drawn — this is the frame a developer most wants
        # labelled, because an empty overlay otherwise looks like a crash.
        cv2.putText(canvas, f"Status: {result.status.value.upper()}", (20, 40), font, 1.0, _RED, 2)
        return canvas

    finger = result.finger_point
    fx, fy = int(finger.x), int(finger.y)

    if mode == "debug":
        for rank, candidate in enumerate(result.candidate_scores):
            word = candidate.word
            colour = _YELLOW if rank == 0 else _GREY
            cv2.rectangle(
                canvas, (word.bbox[0], word.bbox[1]), (word.bbox[2], word.bbox[3]), colour,
                2 if rank == 0 else 1,
            )
            cv2.putText(
                canvas, f"{candidate.total_score:.2f}", (word.bbox[0], word.bbox[1] - 4),
                font, 0.4, colour, 1,
            )

        if finger.direction is not None:
            dx, dy = finger.direction
            end = (int(fx + dx * 100), int(fy + dy * 100))
            cv2.arrowedLine(canvas, (fx, fy), end, _MAGENTA, 2, tipLength=0.2)
            cv2.putText(canvas, "Pointing Vector", (end[0] + 5, end[1]), font, 0.4, _MAGENTA, 1)

    if mode in ("developer", "debug") and result.candidate_scores:
        # The search polygon is not stored on the result, so it is recomputed from
        # the candidates' geometry. Only an approximation of the real search zone
        # when the candidate list was truncated — good enough to see where the
        # cone pointed, which is all this is for.
        from backend.app.modules.gesture_engine.selector import get_search_polygon

        words = [candidate.word for candidate in result.candidate_scores]
        avg_width = sum(word.width for word in words) / len(words)
        avg_height = sum(word.height for word in words) / len(words)

        polygon = get_search_polygon(finger, avg_height, avg_width, SelectionConfig())
        points = np.array(polygon, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(canvas, [points], isClosed=True, color=_BLUE, thickness=2)
        cv2.putText(
            canvas, "Search Zone", (points[2][0][0], points[2][0][1] - 5), font, 0.5, _BLUE, 1
        )

        line_words = [word for word in words if word.line_index == result.line_index]
        if line_words:
            x_min = min(word.bbox[0] for word in line_words)
            y_min = min(word.bbox[1] for word in line_words)
            x_max = max(word.bbox[2] for word in line_words)
            y_max = max(word.bbox[3] for word in line_words)
            cv2.rectangle(canvas, (x_min, y_min), (x_max, y_max), _CYAN, 1)
            cv2.putText(
                canvas, f"Line {result.line_index}", (x_min, y_min - 5), font, 0.4, _CYAN, 1
            )

    # Fingertip marker, drawn in every mode.
    cv2.circle(canvas, (fx, fy), 8, _RED, -1)
    cv2.circle(canvas, (fx, fy), 10, _WHITE, 1)

    if result.status in (SelectionStatus.SUCCESS, SelectionStatus.LOW_CONFIDENCE):
        if result.selected_word_bbox is not None:
            x_min, y_min, x_max, y_max = result.selected_word_bbox
            cv2.rectangle(canvas, (x_min, y_min), (x_max, y_max), _GREEN, 3)
            label = f"'{result.selected_word}'"
            if result.status is SelectionStatus.LOW_CONFIDENCE:
                label += " (Low Conf)"
            cv2.putText(canvas, label, (x_min, y_min - 8), font, 0.6, _GREEN, 2)

    # Status board.
    cv2.rectangle(canvas, (10, 10), (350, 100), (0, 0, 0), -1)
    status_colour = (
        _GREEN
        if result.status is SelectionStatus.SUCCESS
        else _YELLOW
        if result.status is SelectionStatus.LOW_CONFIDENCE
        else _RED
    )
    cv2.putText(canvas, f"Status: {result.status.value.upper()}", (20, 35), font, 0.5, status_colour, 1)
    cv2.putText(canvas, f"Confidence: {result.confidence:.2f}", (20, 55), font, 0.5, _WHITE, 1)
    cv2.putText(canvas, f"Time: {result.selection_time_ms:.1f} ms", (20, 75), font, 0.5, _WHITE, 1)
    if result.status in (SelectionStatus.SUCCESS, SelectionStatus.LOW_CONFIDENCE):
        cv2.putText(canvas, f"Word: {result.selected_word}", (20, 95), font, 0.5, _GREEN, 1)

    return canvas
