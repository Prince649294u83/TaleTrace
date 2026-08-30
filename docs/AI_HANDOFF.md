# AI Handoff & Technical Context Specification

> **Primary Handoff File:** [AGENTS.md](file:///E:/Projects/TaleTrace/AGENTS.md)
> **Active Working Branch:** `Latest-changes-test` (resolves from `85e83d3f1e73efc4d6bddc48239d08eafe655b29`)
> **Software Shadow Verification Gate:** 100% Green (835 pytest passed, 11 vitest passed, `verify_all.py` passed)
> **Physical Hardware Verification Gate:** Ready for staged execution (Phases C7–C12)

---

## 1. System Overview & Core Philosophy

TaleTrace is a physical smart bookmark powered by an ESP32-CAM, physical buttons (MOMENTARY for gesture word pointing, TOGGLE for Meaning Mode lookup), an OLED display, and audio output (TTS narration and Novel Mode ambient soundscapes).

```text
Physical Hardware Rig (ESP32-CAM + Buttons + OLED + Books)
         │
         ▼  (HTTP REST / JPEG Stream)
Backend Pipeline (FastAPI, Google Vision OCR, MediaPipe Gestures, Groq Key 1/2, SQLite)
         │
         ▼  (REST API / JSON)
Frontend Companion Web App (React, Tailwind CSS, Recharts) - *Review App Only*
```

---

## 2. Invariants, Boundaries & Non-Negotiable Rules

1. **Secrets:** Never commit `.env` or print secret values.
2. **Groq Keys:**
   - `GROQ_API_KEY_1`: AI Engine (Meaning Mode, explanations, end-of-session reviews).
   - `GROQ_API_KEY_2`: Merge Memory (`GroqReconstructor`, text formatting).
   - Strictly segregated. Never merge or share.
3. **Reference Specification:** `backend/app/OCRandGESTURE/` is a frozen reference specification.
4. **Google Vision OCR:** Only production OCR engine. JSON replay is for tests only.
5. **Focus Analytics Terminology:** Strictly "Reading Focus Analysis" and "Possible Idle Time". Never "Distraction Detection".
6. **Valid Frame Gate:** 6-gate checklist mandatory before investigating any gesture or selector algorithm changes:
   - Gate 1: HTTP 200 JPEG.
   - Gate 2: OpenCV decoding.
   - Gate 3: Valid OCR word bounding boxes.
   - Gate 4: Matching coordinate spaces.
   - Gate 5: Fingertip observation.
   - Gate 6: Proof that selector chose the wrong candidate.
7. **Camera Drop Invariant:** A physical `NO_FRAME` is never a gesture failure. It aborts the transaction cleanly as `CAMERA_UNAVAILABLE` without changing the reading pointer or OLED display.
8. **Audio Invariant:** `attempted != spoken`. `words_spoken` only increments and pointer only advances upon complete, uninterrupted playback.

---

## 3. Comprehensive Test Results Across All Phases (Phase A to Phase C6.5)

### A. Phase A Baseline Results (OCR & Bounding Box Sanity)
- **Scope**: Single-image Google Vision OCR parsing, bounding box validation, initial MediaPipe landmark inference on 10 book images.
- **Results**:
  - Image Loading & Normalization: 10/10 PASS.
  - OCR Text Extraction & Box Mapping: 10/10 PASS.
  - Average Word Box Integrity: 100% valid bounding boxes with non-zero area.

### B. Phase B Baseline Results (Golden Corpus & Selection Stability)
- **Corpus Reconciliation**: 10/10 byte-for-byte reconciliation between external corpus and canonical repo (`tests/hardware_corpus/original`).
- **Algorithm Invariance Verification**:
  - Selection Margins: $0.231 \ge 0.20$ safety threshold verified on Page 13 (`challenge` winner).
  - Line Thickness & Touch Sorting: Verified 49-point perturbation grid with zero false jumps.
  - Diagonal Perturbation Stability: Verified 100% stability under $\pm 5\text{px}$ drift.
  - Clean-Page Finding (`7.jpeg` / `page_17_hand.jpg`): Verified contour detector produces raw candidate `(1069.0, 880.0, conf=0.423)` but the selection safety layer rejects it, producing zero spurious TTS jumps.

### C. Phase C & C.5 Results (Live Orchestration & Adversarial Mutation)
- **Execution Matrix**: 62 expected / 62 executed / 62 passed / 0 skipped / 0 failed.
- **Meaning Mode Integration Scenario (Page 13)**:
  - Selected word: `challenge` at `(414.5, 594.5)` $\to$ SUCCESS.
  - AI Calls: 1 Explanation (`GROQ_API_KEY_1`), 1 Summary (`GROQ_API_KEY_1`), 1 Learning Engine (`GROQ_API_KEY_3`).
  - Database Persistence: 1 row persisted with full `review_payload`.
  - Quiz / Flashcard Merging: Served from persisted payload with **0 additional AI calls**.
- **Negative Occlusion Scenario (Page 18)**:
  - Occluded target: `alds` at `(585.0, 749.5)` $\to$ `SAFE_REJECT` (`OCR_FRAGMENT_OCCLUDED`).
  - Invariant: **0 AI explanation calls**, **0 OLED updates**, reading continues uncorrupted.
- **Audio State Machine 5 Scenarios**:
  - `audio_scenario_01` (TTS Only): `TTS: PLAYING`, `Ambient: IDLE` $\to$ PASS.
  - `audio_scenario_02` (Ambient Only): `TTS: IDLE`, `Ambient: PLAYING` $\to$ PASS.
  - `audio_scenario_03` (Concurrent): `TTS: PLAYING`, `Ambient: PLAYING` $\to$ PASS.
  - `audio_scenario_04` (Pause & Resume): Both TTS and Ambient pause on Meaning Mode hold, resume on release, `generation_incremented=True` $\to$ PASS.
  - `audio_scenario_05` (Scene Crossfade): `peaceful -> tavern -> peaceful` crossfade $\to$ PASS.
- **Hardware Supervisor State Transitions**:
  - Live transition sequence (`DISCONNECTED -> READY -> DROPOUT -> RECOVERY`) verified for camera and button peripherals $\to$ PASS.
- **Adversarial Mutation Testing (6/6 Injected Mutations Caught)**:
  - Mutation 1 (Wrong word selection injected) $\to$ **CAUGHT** (FAIL).
  - Mutation 2 (Omitted AI explanation call) $\to$ **CAUGHT** (FAIL).
  - Mutation 3 (Inverted audio playback state) $\to$ **CAUGHT** (FAIL).
  - Mutation 4 (Corrupted DB lookup payload) $\to$ **CAUGHT** (FAIL).
  - Mutation 5 (Hardware supervisor state desync) $\to$ **CAUGHT** (FAIL).
  - Mutation 6 (Deliberately skipped test case) $\to$ **CAUGHT** (FAIL).

### D. Phase C1–C6.5 Results (Forensic Remediation & Software Shadow Freeze)
- **Test Suite Results**:
  - `python -m pytest`: **835 passed**, 3 warnings in **10.46s** (100% green).
  - Frontend Vitest: **11 passed** in **300ms** (100% green).
  - `python scripts/verify_all.py`: **PASS** exit 0 in **17.4s**.
- **The 9 New Forensic Test Cases**:
  1. `test_failed_synthesis_never_counts_as_spoken_and_never_advances_pointer`: PASS.
  2. `test_successful_synthesis_counts_as_spoken_and_advances_pointer`: PASS.
  3. `test_synthesis_success_but_playback_sink_failure_never_counts_as_spoken_and_never_advances_pointer`: PASS.
  4. `test_offline_speech_provider_synthesizes_wav_bytes`: PASS.
  5. `test_playback_engine_with_offline_provider_lifecycle`: PASS.
  6. `test_offline_provider_meaning_mode_immediate_stop`: PASS.
  7. `test_offline_provider_in_flight_synthesis_task_cancellation`: PASS.
  8. `test_groq_reconstructor_sends_max_tokens_candidate_850`: PASS.
  9. `test_20_sequence_merge_stress_preserves_reading_continuity`: PASS.
- **Groq 20-Sequence Merge Stress Test Metrics**:
  - Total requests: 20 sequential merge frames.
  - Successful completions: 14 frames.
  - Transient 429s retried within budget: 3 frames (waited 0.01s).
  - Long 429s immediately falling back to raw OCR: 3 frames.
  - Maximum blocking latency: **18.2ms** (budget: 1500ms).
  - Text loss: **0% (100% monotonic growth)**.
  - Pointer progression: 20/20 valid monotonic offsets.

---

## 4. Physical Rig Execution Sequence (Phases C7–C12)

```text
Phase C7: Preflight & Environment Cleanup
  ├── Remove temporary test variables: ESP32_CAM_CAPTURE_URL, ESP32_BUTTONS_URL, etc.
  └── python -m backend.app.live_session --check

Phase C8: Physical Camera & Coordinate Gate
  └── python scripts/diagnose_camera.py (verify P99, failure rate, OpenCV decode)

Phase C9: Staged Gesture & Reading Runs
  ├── 5s smoke test:      python -m backend.app.live_session --buttons hardware --seconds 5
  ├── 30s controlled run: python -m backend.app.live_session --buttons hardware --seconds 30
  ├── 60s integrated run: python -m backend.app.live_session --buttons hardware --seconds 60
  └── 120s final run:     python -m backend.app.live_session --buttons hardware --seconds 120

Phase C10: Physical Audio Validation (Edge TTS + SAPI5 Fallback + Ambient Crossfade)
Phase C11: Physical Meaning Mode & OLED Validation (Hold TOGGLE -> Pause both -> OLED text)
Phase C12: 10-Page Continuous Physical Book Rehearsal
```

---

## 5. Physical Incident Replay Schema

If an unexpected behavior occurs during physical testing, record the incident bundle:

```text
incident_bundle_<TX_ID>/
  ├── frame.jpg              # Exact raw JPEG from camera
  ├── frame_sha256.txt       # SHA256 checksum
  ├── ocr.json               # Word bounding boxes
  ├── page_context.json      # Current merge memory text
  ├── gesture.json           # Fingertip coordinates & landmark confidence
  ├── selection.json         # Candidate scores, distances, margins, winner
  ├── button_state.json      # Hardware button states
  ├── audio_state.json       # Provider, generation token, sink state
  ├── ai_trace.json          # Groq input/output tokens, latency
  └── transaction.json       # Telemetry record with ERROR_CLASS & DURATION
```
