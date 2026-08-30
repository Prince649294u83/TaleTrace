# 09. Test Harness Integrity

## Question 2: Can the current shadow harness ever report PASS without executing the real AI / Learning / hardware-state scenarios?

### Evidence
The shadow harness (`scripts/test_test_folder_software.py`) repeatedly defines the expected outcome by hardcoding the variables it assigns as `observed`, rather than probing the actual state of the application after execution.
For example:
```python
observed={"tts_active": True, "ambient_active": True}
```
And:
```python
self.log_ai_call("GROQ_API_KEY_1", ...)
observed={"meaning_explained": True, "ai_calls": len(self.ai_call_log)}
```

### Analysis
**The test harness is structurally compromised.** The reported `58/58 PASS` result does not prove that integration paths executed successfully. It only proves that the test script's internal logic successfully compared the literal values it forced into the `observed` dictionaries. The physical hardware states, learning engine logic, and concurrent audio capabilities are entirely bypassed by these synthetic assumptions.

### Secondary Weaknesses
- **CLI Flags**: `--stage` does not dispatch correctly (always executes all). `--ocr none` quietly falls back to `cached`. `--offline` defaults to True, ignoring the user.
- **Learning Engine Test**: By manually forging a `SessionRow` and skipping `AiBridge.review()`, the learning integration test proves that `review.py` can merge strings, but it explicitly fails to verify that the Groq Key 3 AI pipeline actually functions end-to-end.
