from __future__ import annotations

import math
from typing import Any

from backend.app.modules.gesture_engine.fusion import ObservationFuser
from backend.app.modules.gesture_engine.selection_models import (
    DetectionObservation,
    FingerObservation,
    FingerPoint,
    SelectionConfig,
)
from backend.app.modules.gesture_engine.tracker import GestureMotionTracker

# The MediaPipe Hands graph costs roughly a second to build and is stateless
# across calls in static-image mode, so it is built once and reused.
_HANDS: Any = None
_HANDS_FAILED = False
_GLOBAL_TRACKER = GestureMotionTracker()
_GLOBAL_FUSER = ObservationFuser()


def get_mediapipe_hands(static_image_mode: bool = True) -> Any:
    """The cached Hands graph, or `None` if MediaPipe cannot run here."""
    global _HANDS, _HANDS_FAILED

    if _HANDS is not None or _HANDS_FAILED:
        return _HANDS

    try:
        import mediapipe as mp

        _HANDS = mp.solutions.hands.Hands(
            static_image_mode=static_image_mode,
            max_num_hands=1,
            min_detection_confidence=0.5,
        )
    except Exception:  # pragma: no cover - depends on the host's wheels
        _HANDS_FAILED = True
        _HANDS = None
    return _HANDS


def estimate_direction(
    mcp: tuple[float, float],
    pip: tuple[float, float],
    dip: tuple[float, float],
    tip: tuple[float, float],
) -> tuple[float, float]:
    """Pointing unit vector from the four index-finger joints.

    Weighted 0.5 / 0.3 / 0.2 from the tip segment inward.
    """
    import numpy as np

    dx = 0.5 * (tip[0] - dip[0]) + 0.3 * (dip[0] - pip[0]) + 0.2 * (pip[0] - mcp[0])
    dy = 0.5 * (tip[1] - dip[1]) + 0.3 * (dip[1] - pip[1]) + 0.2 * (pip[1] - mcp[1])

    length = float(np.hypot(dx, dy))
    if length < 1e-5:
        return (0.0, -1.0)
    return (dx / length, dy / length)


def compute_mediapipe_geometric_confidence(
    mcp: tuple[float, float],
    pip: tuple[float, float],
    dip: tuple[float, float],
    tip: tuple[float, float],
    frame_dims: tuple[int, int],
) -> float:
    """Compute geometric landmark confidence based on joint segment lengths and alignment."""
    import numpy as np

    w, h = frame_dims
    # Check if landmarks are strictly within frame bounds
    for pt in (mcp, pip, dip, tip):
        if pt[0] < 0 or pt[0] > w or pt[1] < 0 or pt[1] > h:
            return 0.2

    # Segment lengths
    d_mcp_pip = float(np.hypot(pip[0] - mcp[0], pip[1] - mcp[1]))
    d_pip_dip = float(np.hypot(dip[0] - pip[0], dip[1] - pip[1]))
    d_dip_tip = float(np.hypot(tip[0] - dip[0], tip[1] - dip[1]))

    if d_mcp_pip < 5.0 or d_pip_dip < 4.0 or d_dip_tip < 3.0:
        return 0.3

    # Segment collinearity (straightness vs curl)
    v1 = np.array([pip[0] - mcp[0], pip[1] - mcp[1]]) / d_mcp_pip
    v2 = np.array([dip[0] - pip[0], dip[1] - pip[1]]) / d_pip_dip
    v3 = np.array([tip[0] - dip[0], tip[1] - dip[1]]) / d_dip_tip

    dot1 = float(np.dot(v1, v2))
    dot2 = float(np.dot(v2, v3))

    straightness = max(0.0, 0.5 * (dot1 + dot2))
    return float(np.clip(0.4 + 0.55 * straightness, 0.4, 0.95))


def detect_finger_mediapipe(image: Any, config: SelectionConfig) -> FingerPoint | None:
    """Tier 1: MediaPipe Hands. Returns `None` when no hand is in frame."""
    obs = detect_finger_mediapipe_observation(image, config)
    if obs is None:
        return None

    return FingerPoint(
        x=obs.x,
        y=obs.y,
        confidence=obs.confidence,
        direction=obs.direction or (0.0, -1.0),
        detection_method="mediapipe",
    )


def detect_finger_mediapipe_observation(
    image: Any, config: SelectionConfig, frame_id: int = 0
) -> DetectionObservation | None:
    """Tier 1: MediaPipe Hands returning a DetectionObservation."""
    import cv2

    hands = get_mediapipe_hands()
    if hands is None:
        return None

    height, width = image.shape[:2]
    results = hands.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    if not results.multi_hand_landmarks:
        return None

    landmarks = results.multi_hand_landmarks[0].landmark
    mcp, pip, dip, tip = (
        (landmarks[index].x * width, landmarks[index].y * height) for index in (5, 6, 7, 8)
    )

    geom_conf = compute_mediapipe_geometric_confidence(mcp, pip, dip, tip, (width, height))
    direction = estimate_direction(mcp, pip, dip, tip)

    return DetectionObservation(
        detector_name="mediapipe",
        x=tip[0],
        y=tip[1],
        direction=direction,
        confidence=geom_conf,
        geometry_score=geom_conf,
        direction_confidence=0.9 if direction is not None else 0.4,
        is_predicted=False,
        frame_id=frame_id,
    )


def detect_partial_finger(
    image: Any,
    config: SelectionConfig | None = None,
    active_page_bbox: tuple[int, int, int, int] | None = None,
    prior: tuple[float, float] | None = None,
    frame_id: int = 0,
) -> DetectionObservation | None:
    """Tier 2: Adaptive Partial-Finger CV Detector with PCA orientation and non-color fallback."""
    import cv2
    import numpy as np

    if image is None or not hasattr(image, "shape") or len(image.shape) < 2:
        return None

    height, width = image.shape[:2]

    # Stage A: Skin Segmentation (HSV + YCrCb)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)

    # HSV mask
    mask_hsv = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 15, 35], np.uint8), np.array([22, 255, 255], np.uint8)),
        cv2.inRange(hsv, np.array([165, 15, 35], np.uint8), np.array([180, 255, 255], np.uint8)),
    )
    # YCrCb mask
    mask_ycrcb = cv2.inRange(
        ycrcb, np.array([0, 133, 77], np.uint8), np.array([255, 175, 127], np.uint8)
    )

    mask = cv2.bitwise_and(mask_hsv, mask_ycrcb)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # If skin mask found nothing, only run edge fallback if there is an active tracking prior
    if (not contours or max((cv2.contourArea(c) for c in contours), default=0) < 500) and prior is not None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 40, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return None

    # Filter contours for valid finger blobs
    min_area = max(200, int(0.0015 * width * height))
    min_dim = max(8, int(0.015 * min(width, height)))
    border_margin = max(15, int(0.07 * min(width, height)))

    candidates = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or area > (0.25 * width * height):
            continue
        hull = cv2.convexHull(c)
        hull_area = cv2.contourArea(hull)
        solidity = float(area) / hull_area if hull_area > 0 else 0.0

        _, _, bw, bh = cv2.boundingRect(c)
        if min(bw, bh) < min_dim:
            continue
        aspect = max(float(bw) / max(bh, 1), float(bh) / max(bw, 1))
        if aspect < 1.35 and area < (0.05 * width * height):
            continue

        prior_score = 1.0
        if prior is not None:
            M = cv2.moments(c)
            if M["m00"] > 0:
                cx = M["m10"] / M["m00"]
                cy = M["m01"] / M["m00"]
                dist = math.hypot(cx - prior[0], cy - prior[1])
                prior_score = float(np.exp(-dist / 200.0))

        candidates.append((c, area, solidity, aspect, prior_score))

    if not candidates:
        return None

    best_candidate = max(
        candidates,
        key=lambda item: (item[1] ** 0.5) * item[2] * min(item[3], 4.0) * item[4],
    )
    contour, area, solidity, aspect, _ = best_candidate

    pts = contour.reshape(-1, 2).astype(np.float32)
    if len(pts) < 5:
        return None

    mean, eigenvectors = cv2.PCACompute(pts, mean=None)
    center = (float(mean[0, 0]), float(mean[0, 1]))
    axis = eigenvectors[0]

    projections = [float(np.dot(p - mean[0], axis)) for p in pts]
    min_idx = int(np.argmin(projections))
    max_idx = int(np.argmax(projections))
    end_a = (float(pts[min_idx, 0]), float(pts[min_idx, 1]))
    end_b = (float(pts[max_idx, 0]), float(pts[max_idx, 1]))
    touches_border_a = (
        end_a[0] <= border_margin
        or end_a[0] >= width - border_margin
        or end_a[1] <= border_margin
        or end_a[1] >= height - border_margin
    )
    touches_border_b = (
        end_b[0] <= border_margin
        or end_b[0] >= width - border_margin
        or end_b[1] <= border_margin
        or end_b[1] >= height - border_margin
    )

    if touches_border_a and touches_border_b:
        return None  # Both ends on margin -> border/corner artifact

    if touches_border_a and not touches_border_b:
        base, tip = end_a, end_b
    elif touches_border_b and not touches_border_a:
        base, tip = end_b, end_a
    else:
        dist_a = math.hypot(end_a[0] - center[0], end_a[1] - center[1])
        dist_b = math.hypot(end_b[0] - center[0], end_b[1] - center[1])
        if dist_a > dist_b or end_a[1] < end_b[1]:
            tip, base = end_a, end_b
        else:
            tip, base = end_b, end_a

    if tip[0] <= border_margin or tip[0] >= width - border_margin or tip[1] <= border_margin or tip[1] >= height - border_margin:
        return None  # Fingertip cannot be within outer page margin

    dir_dx = tip[0] - base[0]
    dir_dy = tip[1] - base[1]
    dir_len = math.hypot(dir_dx, dir_dy)
    direction = (dir_dx / dir_len, dir_dy / dir_len) if dir_len > 1e-4 else (0.0, -1.0)

    confidence = float(np.clip(0.3 + 0.2 * solidity, 0.1, 0.5))

    return DetectionObservation(
        detector_name="contour_fallback",
        x=tip[0],
        y=tip[1],
        direction=direction,
        confidence=confidence,
        geometry_score=solidity,
        direction_confidence=0.5,
        is_predicted=False,
        frame_id=frame_id,
    )


def detect_finger_contour_fallback(image: Any) -> FingerPoint | None:
    """Contour fallback wrapping detect_partial_finger for backwards compatibility."""
    obs = detect_partial_finger(image)
    if obs is None:
        return None

    return FingerPoint(
        x=obs.x,
        y=obs.y,
        confidence=obs.confidence,
        direction=obs.direction or (0.0, -1.0),
        detection_method="contour_fallback",
    )


def detect_finger_fused(
    image: Any,
    config: SelectionConfig | None = None,
    frame_id: int = 0,
    page_id: str = "page_default",
) -> FingerObservation | None:
    """Tier 1 + Tier 2 + Tier 3 Observation Fusion with temporal tracking and hysteresis."""
    cfg = config or SelectionConfig()
    height, width = image.shape[:2] if image is not None and hasattr(image, "shape") else (768, 1024)

    # 1. Tier 1: MediaPipe
    mp_obs: DetectionObservation | None = None
    try:
        mp_obs = detect_finger_mediapipe_observation(image, cfg, frame_id=frame_id)
    except Exception:
        mp_obs = None

    # 2. Tier 2: Partial-Finger CV
    partial_obs: DetectionObservation | None = None
    try:
        prior_pt = (mp_obs.x, mp_obs.y) if mp_obs is not None else None
        partial_obs = detect_partial_finger(image, cfg, prior=prior_pt, frame_id=frame_id)
    except Exception:
        partial_obs = None

    # 3. Tier 3: Motion Tracker
    if mp_obs is None and partial_obs is None:
        _GLOBAL_TRACKER.reset()
        return None

    strongest_obs = mp_obs if (mp_obs is not None and mp_obs.confidence >= 0.5) else partial_obs
    tracker_obs = _GLOBAL_TRACKER.update(strongest_obs, frame_id=frame_id)

    # 4. Fuse observations
    all_obs = [obs for obs in (mp_obs, partial_obs, tracker_obs) if obs is not None]
    return _GLOBAL_FUSER.fuse(all_obs, image_dims=(width, height), frame_id=frame_id, page_id=page_id)


def detect_finger(image: Any, config: SelectionConfig | None = None) -> FingerPoint | None:
    """Detect the fingertip using 3-tier fused detector with backward compatibility."""
    fused_obs = detect_finger_fused(image, config)
    if fused_obs is None:
        return None

    return FingerPoint(
        x=fused_obs.x,
        y=fused_obs.y,
        confidence=fused_obs.confidence,
        direction=fused_obs.direction or (0.0, -1.0),
        detection_method=fused_obs.provenance,
    )

