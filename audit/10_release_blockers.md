# 10. Release Blockers (P0)

The following four issues have been identified as absolute blockers. The system cannot be safely deployed to physical hardware until these are resolved:

1. **`live_session.py` `Path` Runtime Failure**:
   - `live_session.py` references `Path("assets/audio")` but lacks the `from pathlib import Path` import, triggering a fatal `NameError` crash upon ambient asset initialization during hardware startup.
2. **Ambient `SceneController` Disconnected**:
   - The Novel Mode scene intelligence is currently physically disconnected. `SceneController` is initialized without the `AiBridge` in `live_session.py`, forcing it to silently ignore AI calls and repeatedly return the default audio scene.
3. **Shadow Harness False-Positive Architecture**:
   - `test_test_folder_software.py` fakes AI calls and hardcodes `observed={...}` values for critical test paths (audio concurrency, meaning mode lookups, learning engine logic). A passing test suite currently does not guarantee that the integration paths are actually functional.
4. **Corpus Mapping Disagreement (`7.jpeg`)**:
   - The external physical test corpus contains an EXIF-oriented photo for `7.jpeg` that does not contain a finger. The repository's `manifest.json` blindly assumes it is a pointing hand (`page_17_hand.jpg`), enforcing tests against wrong parameters and masking structural coordinate space issues.
