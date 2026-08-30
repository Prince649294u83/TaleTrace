# AGENTS.md — Authoritative Master Handoff for AI Agents

You are picking up a working, rigorously validated system, not a greenfield. Read this file top to bottom before executing any command or editing any code. It exists because several behaviors in this codebase look like bugs and are not, and because a handful of invariant rules were paid for in hours of forensic testing and physical hardware validation.

**Order of reading:**
1. This file (`AGENTS.md` / `docs/AI_HANDOFF.md`)
2. [README.md](file:///E:/Projects/TaleTrace/README.md) §2 (how to run the app and test suites)
3. [docs/architecture.md](file:///E:/Projects/TaleTrace/docs/architecture.md) (module contracts and boundaries)
4. [phase_C_6_5_evidence.md](file:///C:/Users/dell/.gemini/antigravity-ide/brain/5d9a8104-7a2f-41a8-aa60-434c07b75514/phase_C_6_5_evidence.md) (Phase C0–C6.5 evidence freeze)

---

## 1. Executive Context: What TaleTrace Is

TaleTrace is a **physical smart bookmark** for reading physical paper books. An ESP32-CAM clipped to a paper book watches the page, tracks the reader's fingertip, reads the current sentence aloud via TTS narration, provides ambient soundscapes matched to the scene mood in Novel Mode, and explains words upon button press via Meaning Mode (OLED display + AI Engine). Everything the reader does is recorded and persisted to an SQLite database, which a companion React website reviews afterwards.

### Two Asymmetric Halves:
1. **The Device Pipeline (`backend/app/`)**: Where all intelligence lives. Runs locally against the physical rig or simulated session.
2. **The Companion Website (`frontend/`)**: A **review app, not a reading app**. There is **no "start reading" button in the browser and there must never be one**. Reading happens on the physical rig; the companion website analyzes and reviews past reading history.

---

## 2. Ironclad Rules You Cannot Break

These are standing, immutable constraints. Breaking any of these invalidates the release:

1. **Never commit `.env`.** Two `.env` files hold live API credentials: root `.env` and `backend/app/OCRandGESTURE/.env`. Never read, print, or stage them. Report secrets as present/absent booleans only.
2. **Two Groq Keys, Never Shared.**
   - `GROQ_API_KEY_1` is for the **AI Engine only** (Meaning Mode, explanations, review questions).
   - `GROQ_API_KEY_2` is for **Merge Memory / Text Reconstruction only** (`GroqReconstructor`).
   - They must remain separate so rate limits on the live capture merge loop cannot starve the reader's Meaning Mode lookups.
3. **`backend/app/OCRandGESTURE/` is a Frozen Reference Specification.** It is the original prototype and functional spec. Read it; never edit it.
4. **Main Hardware File**: `E:\Projects\TaleTrace\buttons_and_oled.ino` is the authoritative primary hardware firmware file (GPIO4, GPIO5, SH1106 OLED).
5. **Architectural Rule: ESP32 = Peripheral, Laptop = Brain.** ESP32 devices communicate over local LAN only and never touch cloud services. The laptop performs all OpenCV, OCR, MediaPipe, Groq AI, TTS narration, ambient mixing, and database persistence.
6. **Google Vision is the Only Production OCR Engine.** JSON replay is a test-only fixture adapter. Never present replay as a production engine or add third-party OCR engines to production paths.
7. **Terminology: "Reading Focus Analysis" Only.** Never use "Distraction Detection". Idle time is worded "Possible Idle Time" and "reading paused longer than expected" — never accusatory.
8. **Greyscale Images are Unusable.** MediaPipe and the pipeline require color images. For synthetic testing, use `scripts/synthetic_page.py`.
9. **Git Branch & Push Target.** The active working branch is `Latest-changes-test` (or `origin/Latest-changes` when requested). Never push to `main`.
10. **Core Algorithm Freeze.** Do **NOT** modify Gesture Engine mathematics, tracking filters, selector scoring weights, OCR drop-cap heuristics, or database schema without a reproducing test that passes the **Valid Frame Gate**.

---

## 3. The Dual Release Gate Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                       DUAL RELEASE GATE ARCHITECTURE                        │
├──────────────────────────────────────┬──────────────────────────────────────┤
│ 1. SOFTWARE SHADOW RELEASE GATE      │ 2. PHYSICAL HARDWARE RELEASE GATE    │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • Status: 100% PASS (Phases C0–C6.5) │ • Status: PENDING RIG (Phases C7–C12)│
│ • 835/835 Pytest Passing (10.8s)     │ • ESP32-CAM Live Capture Stability   │
│ • 11/11 Frontend Vitest (300ms)      │ • Hardware Buttons & OLED I2C Bus    │
│ • TaleTrace verify_all Gate (17.4s)  │ • Host Audio Output & Sink Latency   │
│ • Deterministic Corpus Replay        │ • 10-Page Physical Book Rehearsal    │
│ • Mutation Testing (6/6 Caught)      │ • Staged Progression (5s->30s->120s) │
└──────────────────────────────────────┴──────────────────────────────────────┘
```
> [!IMPORTANT]
> The Software Shadow Release Gate and Physical Hardware Release Gate are independent. Software tests can never substitute for physical hardware validation.

---

## 4. The Complete Pipeline & Module Boundaries

```text
ESP32-CAM JPEG Frame
  │
  ├── image_receiver/     Accepts HTTP frame from ESP32-CAM / Generic IP camera
  ├── preprocessing/      Deskews, enhances, canonicalizes EXIF orientation
  ├── ocr/                Google Vision API -> RecognizedWord bounding boxes
  ├── gesture_engine/     MediaPipe + fallback -> Fingertip landmark & candidate selection
  ├── merge_memory/       Reconstructs overlapping frames, detects same-page (GROQ_API_KEY_2)
  ├── reading_engine/     Coordinates reading session, pointers, and lookups
  ├── audio_engine/       Edge TTS / SAPI5 Offline Speech + Ambient Soundscapes
  ├── ai_engine/          Meaning Mode contextual explanations & review quizzes (GROQ_API_KEY_1)
  ├── reading_speed/      Calculates baseline WPM, session pace, and difficulty ratings
  ├── focus_analytics/    Computes Reading Focus Analysis & Possible Idle Time
  └── database/           Persists finished session records to SQLite (taletrace.db)
```

### Module Contracts & Key Entry Points

| Module / Entry Point | File Path | Role & Invariants |
|---|---|---|
| **Main Hardware File** | `buttons_and_oled.ino` | Authoritative firmware for GPIO 4, GPIO 5, and SH1106 OLED display. |
| **ESP32-CAM Firmware** | `backend/app/OCRandGESTURE/espcam/Almost_final.ino` | Optical image capture firmware (`GET /capture`). |
| **Live Session** | `backend/app/live_session.py` | Real hardware entry point. `--check` performs preflight. |
| **Simulated Session** | `backend/app/simulated_session.py` | Headless execution without physical peripherals. |
| **FastAPI Backend** | `backend/app/main.py` | Companion API, device endpoints, and CORS config. |
| **Companion API** | `backend/app/api/companion.py` | REST API routes consumed by the React review app. |
| **Speech Providers** | `backend/app/modules/audio_engine/speech_provider.py` | `EdgeSpeechProvider`, `OfflineSpeechProvider` (SAPI5 WAV), `LocalAudioSink`. |
| **Playback Engine** | `backend/app/modules/audio_engine/playback_engine.py` | State machine owning TTS queue, pointer advancement, and audio sink. |
| **Merge Memory** | `backend/app/modules/merge_memory/reconstruction.py` | `GroqReconstructor` with `max_completion_tokens=850` and 1.5s non-blocking budget. |
| **Gesture Engine** | `backend/app/modules/gesture_engine/` | `detector.py`, `selector.py`, `tracker.py`, `transaction.py`. |
| **Analysis Module** | `backend/app/modules/database/analysis.py` | Computes aggregation for companion dashboard charts. |
| **Camera Diagnostics**| `scripts/diagnose_camera.py` | Statistical diagnostic tool (4-layer H1 measurement protocol). |
| **Verification Gate** | `scripts/verify_all.py` | Official end-to-end test runner. |

---

## 5. Comprehensive Test Results Across All Phases (Phase A to Phase C6.5)

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

## 6. The Mandatory Valid Frame Gate & Clean-Page Control

### The 6-Gate Checklist:
Zero gesture or selector algorithm changes are permitted unless **all 6 gates pass**:
1. **Gate 1 (JPEG Integrity)**: Camera returned HTTP 200 with non-empty payload.
2. **Gate 2 (Image Decoding)**: `cv2.imdecode` produced a valid 3-channel BGR matrix.
3. **Gate 3 (OCR Validity)**: OCR returned valid word bounding boxes for the page.
4. **Gate 4 (Coordinate Match)**: Canonical frame coordinate space matches word bounding space.
5. **Gate 5 (Fingertip Landmark)**: Fingertip observation exists.
6. **Gate 6 (Selection Proof)**: Selector logs prove the wrong candidate won despite valid inputs.

### Transaction Contract on Camera Failure:
```text
NO_FRAME
  ├── No OCR called
  ├── No gesture detector called
  ├── No word selector called
  ├── No AI explanation requested
  ├── No OLED display update
  ├── No reading pointer change
  └── Transaction completed with status: CAMERA_UNAVAILABLE
```

### Clean-Page Control (Physical 7.jpeg Equivalent):
A clean book page without a hand present must **never** produce an actionable word selection or spurious TTS jumps. Raw candidates are logged to distinguish perfect rejection from safety-layer candidate filtering.

---

## 7. Staged Physical Hardware Validation & H0–H8 Research Protocols

When validating on the physical hardware rig, execute strictly in this order:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                      STAGED PHYSICAL VALIDATION GATES                       │
├─────────┬─────────────────────────┬─────────────────────────────────────────┤
│ Phase C7│ Physical Preflight      │ Clear test env vars, hardware preflight │
│ Phase H1│ Baseline Load Profiling │ 4-Layer measurement (Idle, Pure, Cadence│
│         │                         │ Contention: scripts/diagnose_camera.py) │
│ Phase C8│ Camera & Coordinates    │ Live frame capture & canonical transform│
│ Phase C9│ Physical Gesture Gates  │ 5s smoke -> 30s -> 60s -> 120s final    │
│ Phase C10 Physical Audio Gates    │ Live TTS, ambient crossfade, reconnect  │
│ Phase C11 Physical Meaning/OLED   │ Toggle button -> pause both -> OLED show│
│ Phase C12 Final Rehearsal         │ 10-page continuous physical rehearsal   │
└─────────┴─────────────────────────┴─────────────────────────────────────────┘
```

### Phase C7: Environment Cleanup & Preflight Commands
```powershell
# 1. Remove temporary test/diagnostic environment variables
Remove-Item Env:\ESP32_CAM_CAPTURE_URL -ErrorAction SilentlyContinue
Remove-Item Env:\ESP32_BUTTONS_URL -ErrorAction SilentlyContinue
Remove-Item Env:\AUDIO_PROVIDER -ErrorAction SilentlyContinue

# 2. Run Hardware Preflight Check
python -m backend.app.live_session --check
```

### Phase H1: Camera Diagnostics on the Rig
```powershell
# Run the 4-layer statistical camera diagnostic tool
python scripts/diagnose_camera.py
```

### Phase C9–C12: Staged Rig Reading Runs
```powershell
# 5-second smoke test
python -m backend.app.live_session --buttons hardware --seconds 5

# 30-second controlled reading run
python -m backend.app.live_session --buttons hardware --seconds 30

# 60-second integrated reading run
python -m backend.app.live_session --buttons hardware --seconds 60

# 120-second final physical rehearsal
python -m backend.app.live_session --buttons hardware --seconds 120
```

---

## 8. Physical Incident Replay Bundle Schema

If an incident occurs on the physical rig, export it as a self-contained bundle for offline reproduction:

```text
incident_bundle_<TX_ID>/
  ├── frame.jpg              # Exact raw JPEG frame captured from camera
  ├── frame_sha256.txt       # SHA256 checksum of the captured JPEG
  ├── ocr.json               # Word bounding boxes and OCR response
  ├── page_context.json      # Current merge memory and paragraph text
  ├── gesture.json           # Raw fingertip coordinates, landmarks, and source
  ├── selection.json         # Candidate scores, distances, margins, winner
  ├── button_state.json      # GPIO button states (MOMENTARY, TOGGLE)
  ├── audio_state.json       # Active provider, audio generation token, sink state
  ├── ai_trace.json          # Groq input/output tokens, latency, finish reason
  └── transaction.json       # Structured telemetry record with ERROR_CLASS & DURATION
```

---

## 9. Invariants that Look Like Bugs (Do Not Change!)

1. **`session_wpm == 0.0` is a Sentinel, Not a Speed.** A session shorter than 1000ms cannot be timed. It reports `0.0` (not measurable) and is excluded from daily means (`wpm: null`). Never render as `0`.
2. **`DifficultyLevel.UNKNOWN` is a Refusal to Rate.** It serializes to `null`, never to "Medium".
3. **Three Different Word Counts:**
   - `words_read`: How far the reader progressed in the text.
   - `words_spoken`: How many words TTS narration actually spoke aloud.
   - `reading_time_ms`: Duration of narration, not reading duration.
   - Never merge them or recalculate reading speed from TTS narration.
4. **SQLite UTC Timestamps are Naive.** SQLite has no timezone type. Always use `as_utc` and `.astimezone(timezone.utc).replace(tzinfo=None)` when storing datetimes.
5. **Difficulty Means Use `_round_half_up`.** Banker's rounding breaks ties to even numbers; TaleTrace breaks ties toward the harder read to avoid underestimating difficulty.
6. **No `words_read` Filter in Analysis.** Short reading sessions are legitimate and must never be hidden by arbitrary word thresholds.
7. **Analysis Has No Fallback Mock.** If `/api/analysis` fails, the companion page shows an explicit error state rather than fabricating numbers with `Math.random()`.

---

## 10. Traps and Troubleshooting Guide

- **Port 8000 Already in Use:** `[WinError 10013]` is a leftover backend process, not a firewall issue. Run `netstat -ano | findstr :8000` and `taskkill /F /PID <pid>`.
- **Git Bash Slashes:** Git Bash mangles `/F` into `F:/`. Use double slashes: `taskkill //F //T //PID <pid>`.
- **Windows Console cp1252:** Em dashes in `print()` strings render as `?`. Keep unicode in comments/docstrings; use ASCII in printed logs.
- **Never Hardcode 5173:** Vite picks the next free port if 5173 is occupied. Target `127.0.0.1`, not `localhost` (to avoid IPv6 `::1` resolution).
- **MediaPipe Pinned:** `mediapipe == 0.10.21`. Upgrading breaks gesture detection silently.
- **`npm` is `npm.cmd` on Windows:** In Python `subprocess`, invoke `shutil.which("npm")` and kill with `taskkill /T /F /PID`.
- **Recharts in JSDOM:** Recharts renders empty SVGs in JSDOM due to 0x0 container size; test the `api.js` adapter directly with `vitest`.
- **Working Style (Ponytail Mode):** Root causes over call-site guards. Boring over clever. Run `python -m pytest` before declaring any task done.
