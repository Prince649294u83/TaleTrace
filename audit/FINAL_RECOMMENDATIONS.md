# Final Recommendations & Forensic Conclusions

## Forensic Answers to the 5 Core Questions

1. **Why does the repository claim 7.jpeg is a hand image when the actual supplied 7.jpeg is not?**
   - The test harness blindly trusts the `manifest.json` alias (`page_17_hand.jpg`) and evaluates the output against it, even when the external file (`7.jpeg`) loaded off disk is completely different.
2. **Can the current shadow harness ever report PASS without executing the real AI / Learning / hardware-state scenarios?**
   - **Yes.** The shadow harness explicitly hardcodes successful outcomes (e.g. `observed={"tts_active": True, "ambient_active": True}`) and mocks AI calls internally, completely bypassing real execution paths and rendering the 58/58 PASS result unreliable as a release gate.
3. **Does the live ambient engine actually receive the AiBridge?**
   - **No.** `live_session.py` instantiates `SceneController()` without providing the `ai` argument. The controller immediately returns `_last_valid` (neutral narration) because `self.ai is None`.
4. **Can the current partial-finger detector incorrectly infer the finger endpoint/direction?**
   - **Yes.** The logic `if dist_a > dist_b or end_a[1] < end_b[1]:` forces a strict upward Y-bias when neither endpoint touches the frame edge. If the camera is rotated 90 degrees or inverted, this heuristic fails entirely.
5. **Does the physical-camera coordinate space actually match the OCR/selector coordinate space?**
   - **No.** There is no single authoritative orientation correction applied at frame capture. The shadow harness claims `detect_orientation = True`, but the underlying provider configurations do not enforce this, meaning EXIF-rotated images (like `7.jpeg`) throw the coordinate spaces into misalignment.

## Next Steps

Do **NOT** rewrite the core gesture or selection algorithms yet. 

Instead, our immediate corrective implementation plan should focus entirely on:
1. **Fixing the P0 Blockers**: Importing `Path`, passing `AiBridge` to the `SceneController`, and fixing the shadow harness so it asserts on real runtime states instead of forged observations.
2. **Enforcing Coordinate Space Integrity**: Ensuring EXIF orientation is decoded and standardized at the earliest camera capture boundary, so all downstream consumers (OCR, UI, Gesture) share a unified grid.
3. **Removing Directional Bias**: Upgrading the partial-finger detector to rely on purely geometric/contour data, stripping the hardcoded upward (`Y`) bias.
4. **Database & Auth Hardening**: Injecting the missing foreign key constraints onto SQLite connections and updating the `getMe()` logic in the frontend to correctly identify backend outages versus auth failures.
