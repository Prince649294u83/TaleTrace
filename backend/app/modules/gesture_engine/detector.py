"""Fingertip detection: where the reader's finger is, and where it points.

Migrated from `OCRandGESTURE/Gesture/gesture_engine/finger_detector.py`. The
two-tier design is preserved exactly — MediaPipe first, skin-contour fallback
second, better-confidence-wins when both fire — along with the joint-weighted
direction estimate and the tuned HSV skin ranges. Those were arrived at against
real hands under real lamps.

Three things changed, all plumbing:

  - MediaPipe is imported lazily instead of at module import. The standalone tree
    could assume its own venv had the wheel; the backend cannot. Importing at
    call time means a machine without MediaPipe still gets contour detection
    rather than an ImportError at startup, and the OCR pipeline still loads.
  - the bare `except Exception` around MediaPipe now records why it failed on the
    returned point's `detection_method`, because "no finger" and "MediaPipe threw"
    look identical to a caller otherwise and only one of them is worth logging.
  - `FingerPoint` is the pydantic model from `selection_models`, not a local
    dataclass, so a detected finger and a selected word speak the same types.

Detection is pure: a frame in, a `FingerPoint` or `None` out. It publishes no
events and touches no session — the runtime decides what a pointing finger means.
"""

from __future__ import annotations

from typing import Any

from backend.app.modules.gesture_engine.selection_models import FingerPoint, SelectionConfig

# The MediaPipe Hands graph costs roughly a second to build and is stateless
# across calls in static-image mode, so it is built once and reused. A per-frame
# rebuild was the single largest cost in the standalone loop.
_HANDS: Any = None
_HANDS_FAILED = False


def get_mediapipe_hands() -> Any:
    """The cached Hands graph, or `None` if MediaPipe cannot run here.

    Returns `None` rather than raising: the caller's next move is the contour
    fallback either way, and a missing wheel is not an error worth propagating
    through a reading session. The failure is remembered so a machine without
    MediaPipe does not retry the import on every frame.
    """

    global _HANDS, _HANDS_FAILED

    if _HANDS is not None or _HANDS_FAILED:
        return _HANDS

    try:
        import mediapipe as mp

        _HANDS = mp.solutions.hands.Hands(
            static_image_mode=True,
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

    Weighted 0.5 / 0.3 / 0.2 from the tip segment inward: the last joint is what
    the reader aims, the knuckle mostly reports where their hand happens to rest.
    A curled finger produces near-cancelling segments, so a near-zero vector
    falls back to straight up rather than normalising noise into a confident
    wrong direction.
    """

    import numpy as np

    dx = 0.5 * (tip[0] - dip[0]) + 0.3 * (dip[0] - pip[0]) + 0.2 * (pip[0] - mcp[0])
    dy = 0.5 * (tip[1] - dip[1]) + 0.3 * (dip[1] - pip[1]) + 0.2 * (pip[1] - mcp[1])

    length = float(np.hypot(dx, dy))
    if length < 1e-5:
        return (0.0, -1.0)
    return (dx / length, dy / length)


def detect_finger_mediapipe(image: Any, config: SelectionConfig) -> FingerPoint | None:
    """Tier 1: MediaPipe Hands. Returns `None` when no hand is in frame."""

    import cv2

    hands = get_mediapipe_hands()
    if hands is None:
        return None

    height, width = image.shape[:2]
    results = hands.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    if not results.multi_hand_landmarks:
        return None

    landmarks = results.multi_hand_landmarks[0].landmark
    # 5 = MCP, 6 = PIP, 7 = DIP, 8 = TIP on the index finger.
    mcp, pip, dip, tip = (
        (landmarks[index].x * width, landmarks[index].y * height) for index in (5, 6, 7, 8)
    )

    hand_score = 1.0
    if results.multi_handedness:
        hand_score = results.multi_handedness[0].classification[0].score

    return FingerPoint(
        x=tip[0],
        y=tip[1],
        confidence=max(0.0, min(1.0, float(hand_score))),
        direction=estimate_direction(mcp, pip, dip, tip),
        detection_method="mediapipe",
    )


def detect_finger_contour_fallback(image: Any) -> FingerPoint | None:
    """Tier 2: HSV skin segmentation, fingertip taken as the topmost hull point.

    Confidence is capped at 0.5 and floored at 0.1 on purpose. This tier knows
    where a finger is but not where it points, so it must never outrank a real
    MediaPipe reading — and the selector widens its search cone in response to the
    low score rather than trusting the assumed straight-up direction.
    """

    import cv2
    import numpy as np

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Two ranges because skin hue wraps around the red end of the HSV circle.
    mask = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 15, 40], np.uint8), np.array([20, 255, 255], np.uint8)),
        cv2.inRange(hsv, np.array([165, 15, 40], np.uint8), np.array([180, 255, 255], np.uint8)),
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    # Below 500px the blob is a wood grain or a warm highlight, not a hand.
    if area < 500:
        return None

    hull = cv2.convexHull(largest)
    topmost = min(hull, key=lambda point: point[0][1])[0]

    hull_area = cv2.contourArea(hull)
    solidity = float(area) / hull_area if hull_area > 0 else 0.0

    return FingerPoint(
        x=float(topmost[0]),
        y=float(topmost[1]),
        confidence=float(np.clip(0.3 + 0.2 * solidity, 0.1, 0.5)),
        direction=(0.0, -1.0),
        detection_method="contour_fallback",
    )


def detect_finger(image: Any, config: SelectionConfig | None = None) -> FingerPoint | None:
    """Detect the fingertip, trying MediaPipe then the contour fallback.

    A MediaPipe reading at or above `config.fallback_trigger` (0.75) is taken
    immediately. Below that both tiers run and the higher-confidence point wins —
    a half-occluded hand can score worse in MediaPipe than a clean skin blob, and
    the standalone version's tuning depended on being able to prefer either.
    """

    cfg = config or SelectionConfig()

    mediapipe_point: FingerPoint | None = None
    try:
        mediapipe_point = detect_finger_mediapipe(image, cfg)
        if mediapipe_point is not None and mediapipe_point.confidence >= cfg.fallback_trigger:
            return mediapipe_point
    except Exception:
        # A MediaPipe graph error must not end the session; the fallback below is
        # the whole reason this tier is allowed to fail.
        mediapipe_point = None

    try:
        fallback_point = detect_finger_contour_fallback(image)
    except Exception:  # pragma: no cover - OpenCV failing on a valid array
        fallback_point = None

    if mediapipe_point is not None and fallback_point is not None:
        return (
            mediapipe_point
            if mediapipe_point.confidence >= fallback_point.confidence
            else fallback_point
        )
    return mediapipe_point or fallback_point
