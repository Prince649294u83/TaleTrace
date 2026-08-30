# 04. Audio Findings

## Question 3: Does the live ambient engine actually receive the AiBridge?

### Evidence
In `backend/app/live_session.py`, around line 397, the live session constructs the Scene Controller via:
```python
scene_controller = SceneController()
```
No `ai` argument (the `AiBridge`) is passed to it, even though the `ReadingRuntime` receives the `AiBridge`.

In `backend/app/modules/audio_engine/scene_controller.py`, `SceneController.evaluate()` explicitly checks:
```python
if self.ai is None:
    return self._last_valid
```

### Analysis
The **Live Ambient Scene AI is completely disconnected**. Because `scene_controller` receives no AI bridge, it never invokes Novel Mode AI. It will always immediately return the default/last valid scene. The current production execution will not dynamically change ambient scenes based on the read paragraph text.

### Additional Findings
- **Stale Scene Cache**: `SceneController.invalidate()` clears the cache but explicitly does not cancel in-flight tasks. A task resolving after a page turn can write stale scene decisions back into `_last_valid`.
- **Audio Output Verification**: While the `AudioRuntimeState` tracks state (ambient_expected_to_play, output device), software state does not guarantee physical audibility. The system currently lacks physical speaker validation.
