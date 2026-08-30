# 03. Gesture Engine Findings

## Question 4: Can the current partial-finger detector incorrectly infer the finger endpoint/direction?

### Evidence
In `backend/app/modules/gesture_engine/detector.py`, the endpoint decision for a partial finger evaluates:
```python
if dist_a > dist_b or end_a[1] < end_b[1]:
    tip = end_a
```
### Analysis
This logic introduces an explicit upward vertical bias (`end_a[1] < end_b[1]`). When neither endpoint touches a border (or if distances are otherwise equivalent), the detector automatically assumes the uppermost point is the fingertip. 

Because of this heuristic, the partial-finger detector is **not direction-invariant**. If the camera is rotated or the reader points sideways or downwards, the system can systematically misidentify the finger direction. This completely undermines the physical tracking of isolated fingers.

### Secondary Weaknesses
1. **Confidence Capping**: The partial-finger confidence is hard-capped: `confidence = clip(0.3 + 0.2 * solidity, ..., 0.5)`. Because the MediaPipe fallback caps confidence at `0.5`, the selection system will structurally disadvantage partial-finger detections against any other noise, even when the finger is the only valid subject in frame.
2. **Global State Leaks**: The detector uses `_GLOBAL_TRACKER = GestureMotionTracker()` and `_GLOBAL_FUSER = ObservationFuser()`. This global state can leak across sessions and pages, creating non-deterministic runtime tracking behavior.
3. **Coasting Broken**: The temporal tracker is immediately reset (`_GLOBAL_TRACKER.reset()`) if both detectors miss in a single frame, breaking the intended ability to coast through short physical occlusions.
4. **Upward Selection Bias**: The `get_search_polygon()` defaults to `(0.0, -1.0)` (UP) when directionality is lost, guessing an unsafe direction instead of rejecting the frame.
