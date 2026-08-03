"""Word selection: which word is the reader pointing at.

Migrated from `OCRandGESTURE/Gesture/gesture_engine/word_selector.py`. The
algorithm is preserved as written — the two-stage search (direct touch first,
then a directional cone), the per-line and per-word scoring, the sentence
context reconstruction. That logic was tuned against real pages and rewriting it
would throw away the tuning. What changed is plumbing:

  - words are `ocr.models.RecognizedWord` now, not a duplicate local `OCRWord`
  - rebuilt words are created with `model_copy`, not by hand-listing every field
  - the numpy `np.mean` on a single-element list is replaced with plain division
    (numpy would emit a deprecation warning and it was doing work for nothing)

`select_intended_word` is pure: given a finger and the words, it returns a
verdict. It cannot reach the Audio Engine, the reading pointer, or the session —
which is exactly what makes the "publish events, never call modules" rule
enforceable. The caller decides what a SUCCESS means.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.modules.gesture_engine.selection_models import (
    FingerPoint,
    ScoredCandidate,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
    SelectionStrategy,
    TextLine,
)
from backend.app.modules.ocr.models import RecognizedWord

if TYPE_CHECKING:
    from collections.abc import Sequence


def group_ocr_words(
    ocr_words: Sequence[RecognizedWord], config: SelectionConfig
) -> list[TextLine]:
    """Group recognised words into lines, then paragraphs.

    Two paths. If the provider already assigned every word a `line_index`
    (Google Vision does), that grouping is trusted and words are sorted left to
    right within it. Otherwise lines are clustered by vertical proximity — the
    case for Paddle, which reports boxes but no line structure.
    """

    words = list(ocr_words)
    if not words:
        return []

    if all(word.line_index != -1 for word in words):
        lines_map: dict[int, list[RecognizedWord]] = {}
        for word in words:
            lines_map.setdefault(word.line_index, []).append(word)

        text_lines: list[TextLine] = []
        for line_index, members in sorted(lines_map.items()):
            ordered = sorted(members, key=lambda word: word.center_x)
            for index, word in enumerate(ordered):
                ordered[index] = word.model_copy(
                    update={"word_index": index if word.word_index == -1 else word.word_index}
                )
            y_center = sum(word.center_y for word in ordered) / len(ordered)
            text_lines.append(
                TextLine(
                    words=tuple(ordered),
                    y_center=y_center,
                    bbox=_line_bbox(ordered),
                    line_index=line_index,
                    paragraph_index=ordered[0].paragraph_index,
                )
            )
        return text_lines

    # Provider gave no line structure: cluster by vertical overlap.
    avg_height = sum(word.height for word in words) / len(words)
    tolerance = config.line_cluster_tolerance * avg_height

    clusters: list[list[RecognizedWord]] = []
    for word in sorted(words, key=lambda w: w.center_y):
        placed = False
        for cluster in clusters:
            avg_y = sum(item.center_y for item in cluster) / len(cluster)
            if abs(word.center_y - avg_y) <= tolerance:
                cluster.append(word)
                placed = True
                break
        if not placed:
            clusters.append([word])
    clusters.sort(key=lambda c: sum(w.center_y for w in c) / len(c))

    text_lines = []
    for line_index, cluster in enumerate(clusters):
        ordered = sorted(cluster, key=lambda w: w.center_x)
        for index, word in enumerate(ordered):
            ordered[index] = word.model_copy(
                update={"word_index": index, "line_index": line_index, "paragraph_index": -1}
            )
        text_lines.append(
            TextLine(
                words=tuple(ordered),
                y_center=sum(w.center_y for w in ordered) / len(ordered),
                bbox=_line_bbox(ordered),
                line_index=line_index,
                paragraph_index=-1,
            )
        )

    # Gap-based paragraph grouping: a vertical gap larger than two line heights
    # separates paragraphs, matching the visual rule of thumb for printed text.
    if len(text_lines) <= 1:
        if text_lines:
            text_lines[0] = _with_paragraph(text_lines[0], 0)
        return text_lines

    # The first line has to be seeded explicitly. The loop below only ever
    # assigns to `index + 1`, so without this line 0 keeps the -1 that clustering
    # gave it, lands in no paragraph, and every context lookup for a word on it
    # falls back to the bare line.
    paragraph_index = 0
    text_lines[0] = _with_paragraph(text_lines[0], paragraph_index)

    for index in range(len(text_lines) - 1):
        gap = text_lines[index + 1].y_center - text_lines[index].y_center
        if gap > 2.0 * avg_height:
            paragraph_index += 1
        text_lines[index + 1] = _with_paragraph(text_lines[index + 1], paragraph_index)
    return text_lines


def get_search_polygon(
    finger: FingerPoint,
    avg_line_height: float,
    avg_word_width: float,
    config: SelectionConfig,
):
    """Project the search region as a trapezoid along the pointing direction.

    Wide at the far end because a fingertip is a point but a pointing gesture
    covers an area: the further along the direction vector, the more lateral
    uncertainty. When pointing confidence is low the cone widens by up to 2.5x.
    """

    import numpy as np

    use_direction = _uses_direction(finger, config)
    dx, dy = finger.direction if (use_direction and finger.direction) else (0.0, -1.0)
    nx, ny = -dy, dx  # orthogonal unit vector

    if config.selection_strategy in (SelectionStrategy.TOUCH, SelectionStrategy.STATIC_BOX):
        length = 1.0 * avg_line_height
        buffer = 1.0 * avg_line_height
    else:
        length = config.search_height_above * avg_line_height
        buffer = config.search_height_below * avg_line_height

    start_half_width = avg_word_width / 2.0
    confidence_scale = 1.0 + (1.0 - max(0.0, min(1.0, finger.confidence))) * 1.5
    end_half_width = (config.search_width_ratio * confidence_scale) * avg_word_width / 2.0

    base_x, base_y = finger.x - buffer * dx, finger.y - buffer * dy
    end_x, end_y = finger.x + length * dx, finger.y + length * dy

    return np.array(
        [
            (base_x + start_half_width * nx, base_y + start_half_width * ny),
            (base_x - start_half_width * nx, base_y - start_half_width * ny),
            (end_x - end_half_width * nx, end_y - end_half_width * ny),
            (end_x + end_half_width * nx, end_y + end_half_width * ny),
        ],
        dtype=np.float32,
    )


def get_box_distance(x: float, y: float, bbox: tuple[int, int, int, int]) -> float:
    """Euclidean distance from a point to a box; 0 when the point is inside."""

    import numpy as np

    x_min, y_min, x_max, y_max = bbox
    dx = max(0.0, x_min - x, x - x_max)
    dy = max(0.0, y_min - y, y - y_max)
    return float(np.hypot(dx, dy))


def select_intended_word(
    finger: FingerPoint,
    ocr_words: Sequence[RecognizedWord],
    config: SelectionConfig,
) -> SelectionResult:
    """Select the word the finger is pointing at.

    Stage 1 checks for a direct touch first — a fingertip resting on or within
    half a line height of a word's box wins immediately. Stage 2 projects the
    search cone, picks the line it covers best, and scores each word on that
    line. The result carries the containing sentence so the AI Engine can
    explain the word in context rather than in a vacuum.
    """

    import numpy as np

    text_lines = group_ocr_words(ocr_words, config)
    if not text_lines:
        return SelectionResult(status=SelectionStatus.OCR_EMPTY, finger_point=finger)

    updated_words = [word for line in text_lines for word in line.words]
    avg_word_width = sum(w.width for w in updated_words) / len(updated_words)
    avg_line_height = sum(w.height for w in updated_words) / len(updated_words)

    # Stage 1: direct touch.
    if config.selection_strategy in (
        SelectionStrategy.TOUCH,
        SelectionStrategy.STATIC_BOX,
        SelectionStrategy.HYBRID,
        SelectionStrategy.AUTO,
    ):
        touch_candidates = []
        for word in updated_words:
            distance = get_box_distance(finger.x, finger.y, word.bbox)
            if distance <= 0.5 * avg_line_height:
                touch_candidates.append((distance, word))
        if touch_candidates:
            touch_candidates.sort(
                key=lambda item: (
                    item[0],
                    np.hypot(item[1].center_x - finger.x, item[1].center_y - finger.y),
                )
            )
            best_touch = touch_candidates[0][1]
            min_dist = touch_candidates[0][0]
            best_line = next(
                (line for line in text_lines if line.line_index == best_touch.line_index), None
            )
            if best_line is not None:
                return _finish_selection(
                    best_touch,
                    best_line,
                    text_lines,
                    finger,
                    overall_confidence=float(
                        np.clip(finger.confidence * best_touch.confidence, 0.0, 1.0)
                    ),
                    reason=f"Direct touch selection: finger is within {min_dist:.1f}px of '{best_touch.text}'",
                    config=config,
                )

    # Stage 2: projected search cone.
    use_direction = _uses_direction(finger, config)
    dx, dy = finger.direction if (use_direction and finger.direction) else (0.0, -1.0)

    search_poly = get_search_polygon(finger, avg_line_height, avg_word_width, config)
    best_line: TextLine | None = None
    best_line_score = -1.0

    for line in text_lines:
        words_in_poly = sum(
            1
            for word in line.words
            if _point_in_polygon(word.center_x, word.center_y, search_poly)
        )

        ray_intersects = False
        if use_direction and abs(dy) > 1e-3:
            t = (line.y_center - finger.y) / dy
            buffer_t = config.search_height_below * avg_line_height
            if -buffer_t <= t <= config.search_height_above * avg_line_height:
                rx = finger.x + t * dx
                ray_intersects = line.bbox[0] <= rx <= line.bbox[2]

        y_dist = abs(line.y_center - finger.y)
        y_score = np.exp(-y_dist / (avg_line_height * 2.0))

        if use_direction:
            is_correct_direction = (line.y_center < finger.y) if dy < 0 else (line.y_center > finger.y)
            direction_bias = 1.0 if is_correct_direction else 0.2
        else:
            direction_bias = 1.0

        line_score = (
            words_in_poly * 10.0 + (5.0 if ray_intersects else 0.0) + y_score * 3.0 * direction_bias
        )
        if line_score > best_line_score:
            best_line_score = line_score
            best_line = line

    if best_line is None or best_line_score < 0.1:
        return SelectionResult(
            status=SelectionStatus.NO_WORD_FOUND,
            finger_point=finger,
            selection_reason="No text line intersected with search region",
        )

    # Weights depend on the strategy; touch modes zero the directional terms.
    strategy = config.selection_strategy
    if strategy in (SelectionStrategy.TOUCH, SelectionStrategy.STATIC_BOX):
        v_weight, h_weight, d_weight, o_weight = 0.0, 0.20, 0.0, 0.80
    elif strategy in (SelectionStrategy.POINT, SelectionStrategy.DIRECTION_CONE):
        v_weight, h_weight, d_weight, o_weight = 0.40, 0.10, 0.50, 0.00
    elif finger.direction is None:
        v_weight = config.vertical_bias + config.direction_weight / 2.0
        h_weight = config.horizontal_weight + config.direction_weight / 2.0
        d_weight = 0.0
        o_weight = config.overlap_weight
    else:
        v_weight = config.vertical_bias
        h_weight = config.horizontal_weight
        d_weight = config.direction_weight
        o_weight = config.overlap_weight

    nx, ny = -dy, dx
    scored: list[ScoredCandidate] = []
    for word in best_line.words:
        wx, wy = word.center_x - finger.x, word.center_y - finger.y
        parallel = wx * dx + wy * dy
        perp = wx * nx + wy * ny

        vertical_score = 1.0 if parallel > 0 else float(np.exp(-abs(parallel) / avg_line_height))
        horizontal_score = float(np.exp(-abs(perp) / (avg_word_width * 1.5)))

        norm = np.hypot(wx, wy)
        if norm > 1e-3:
            cos_theta = (wx * dx + wy * dy) / norm
            direction_score = float(max(0.0, cos_theta) ** 2) if finger.direction is not None else 0.0
        else:
            direction_score = 1.0 if finger.direction is not None else 0.0

        if word.bbox[0] <= finger.x <= word.bbox[2] and word.bbox[1] <= finger.y <= word.bbox[3]:
            overlap_score = 1.0
        else:
            dx_box = max(0.0, word.bbox[0] - finger.x, finger.x - word.bbox[2])
            dy_box = max(0.0, word.bbox[1] - finger.y, finger.y - word.bbox[3])
            overlap_score = float(np.exp(-np.hypot(dx_box, dy_box) / (avg_line_height * 0.5)))

        scored.append(
            ScoredCandidate(
                word=word,
                vertical_score=vertical_score,
                horizontal_score=horizontal_score,
                direction_score=direction_score,
                overlap_score=overlap_score,
                total_score=(
                    v_weight * vertical_score
                    + h_weight * horizontal_score
                    + d_weight * direction_score
                    + o_weight * overlap_score
                ),
                scoring_reason=(
                    f"V-score: {vertical_score:.2f}, H-score: {horizontal_score:.2f}, "
                    f"Dir-score: {direction_score:.2f}, Overlap: {overlap_score:.2f}"
                ),
            )
        )

    scored.sort(key=lambda candidate: candidate.total_score, reverse=True)
    if not scored:
        return SelectionResult(
            status=SelectionStatus.NO_WORD_FOUND,
            finger_point=finger,
            selection_reason="No candidate words scored on chosen line",
        )

    best = scored[0].word
    second_best = scored[1].total_score if len(scored) > 1 else 0.0
    separation = 1.0 - (second_best / scored[0].total_score) if scored[0].total_score > 0 else 0.0
    overall_confidence = float(
        np.clip(
            scored[0].total_score * finger.confidence * best.confidence * separation,
            0.0,
            1.0,
        )
    )

    return _finish_selection(
        best,
        best_line,
        text_lines,
        finger,
        overall_confidence=overall_confidence,
        reason=(
            f"Selected '{best.text}' (score={scored[0].total_score:.2f}): "
            f"Separation={separation:.2f}. "
            f"Method={finger.detection_method} (finger_conf={finger.confidence:.2f})."
        ),
        config=config,
        candidate_scores=scored[:5],
    )


def _finish_selection(
    word: RecognizedWord,
    line: TextLine,
    all_lines: list[TextLine],
    finger: FingerPoint,
    *,
    overall_confidence: float,
    reason: str,
    config: SelectionConfig,
    candidate_scores: list[ScoredCandidate] | None = None,
) -> SelectionResult:
    """Assemble the result: line, paragraph, and the containing sentence."""

    paragraph_words = [
        member for text_line in all_lines if text_line.paragraph_index == line.paragraph_index
        for member in text_line.words
    ]

    sentences: list[str] = []
    current: list[str] = []
    target_sentence = -1
    for member in paragraph_words:
        current.append(member.text)
        if _ends_sentence(member.text):
            sentences.append(" ".join(current))
            if (
                member.line_index == word.line_index
                and member.word_index == word.word_index
            ):
                target_sentence = len(sentences) - 1
            current = []
    if current:
        sentences.append(" ".join(current))
        for member in paragraph_words[len(paragraph_words) - len(current):]:
            if member.line_index == word.line_index and member.word_index == word.word_index:
                target_sentence = len(sentences) - 1
                break

    status = (
        SelectionStatus.SUCCESS
        if overall_confidence >= config.confidence_threshold
        else SelectionStatus.LOW_CONFIDENCE
    )

    return SelectionResult(
        status=status,
        selected_word=word.text,
        selected_line=line.text,
        selected_line_words=tuple(member.text for member in line.words),
        selected_paragraph=" ".join(member.text for member in paragraph_words),
        context=sentences[target_sentence] if target_sentence != -1 else line.text,
        confidence=overall_confidence,
        finger_point=finger,
        selected_word_bbox=word.bbox,
        word_index=word.word_index,
        line_index=word.line_index,
        paragraph_index=word.paragraph_index,
        candidate_scores=tuple(candidate_scores or []),
        selection_reason=reason,
    )


def _ends_sentence(text: str) -> bool:
    """Whether a word's trailing punctuation closes a sentence."""

    if not text:
        return False
    return text[-1] in ".!?" or (len(text) > 1 and text[-2] in ".!?" and text[-1] in "\"'")


def _uses_direction(finger: FingerPoint, config: SelectionConfig) -> bool:
    return (
        config.selection_strategy
        in (SelectionStrategy.DIRECTION_CONE, SelectionStrategy.POINT, SelectionStrategy.HYBRID)
        or (
            config.selection_strategy == SelectionStrategy.AUTO
            and finger.direction is not None
            and finger.detection_method == "mediapipe"
        )
    )


def _with_paragraph(line: TextLine, paragraph_index: int) -> TextLine:
    """Stamp a paragraph index onto a line and every word in it.

    Both carry it because the two are read by different steps: line selection
    filters lines by paragraph, while sentence reconstruction walks words. Setting
    it on only one is what left the first line orphaned from its own paragraph.
    """

    return line.model_copy(
        update={
            "paragraph_index": paragraph_index,
            "words": tuple(
                word.model_copy(update={"paragraph_index": paragraph_index})
                for word in line.words
            ),
        }
    )


def _line_bbox(words: Sequence[RecognizedWord]) -> tuple[int, int, int, int]:
    x_min = min(word.bbox[0] for word in words)
    y_min = min(word.bbox[1] for word in words)
    x_max = max(word.bbox[2] for word in words)
    y_max = max(word.bbox[3] for word in words)
    return (x_min, y_min, x_max, y_max)


def _point_in_polygon(x: float, y: float, polygon) -> bool:
    import cv2

    return cv2.pointPolygonTest(polygon, (x, y), False) >= 0
