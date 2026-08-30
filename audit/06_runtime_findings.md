# 06. Runtime Findings

## Startup & Execution

### Evidence
The core execution script `live_session.py` has a critical startup bug:
```python
Path("assets/audio")
```
The `Path` library (from `pathlib`) is used but not imported at the top of the file, resulting in a fatal `NameError` if the code path reaches the ambient asset initialization.

### Additional Findings
- **Synchronous Hardware Probes**: `_camera_live()`, `_buttons_live()`, and `preflight()` all utilize blocking network calls (`time.sleep()`). Because `companion.py` calls these directly in API endpoints, the FastAPI server can block the web companion dashboard from loading.
- **Learning Engine Side Effects**: In `ai_bridge.review()`, if the summary fails, the code continues and generates learning quizzes anyway. This creates an inconsistent internal state where `AiOutcome.ok` reflects a failure but downstream artifacts are generated.
- **Hardware Mode Misalignment**: `buttons_mode="auto"` allows a hybrid real-camera + virtual-buttons execution mode. This violates strict physical testing environments, allowing developers to believe they are running a full physical test when buttons are simulated.
