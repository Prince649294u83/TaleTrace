# Module contracts

Every section lists **inputs**, **outputs**, and **dependencies**. “Depends on”
means contract-level dependency, not a concrete implementation dependency.

## `api`

**Role:** HTTP composition and health exposure.

**Inputs:** HTTP requests routed to module routers, including JSON request
schemas and dependency-provided session context.

**Outputs:** `ResponseEnvelope[T]` responses and health status. Router prefixes
currently include `/ai`, `/gesture`, `/ocr`, `/session`, `/audio-engine`,
`/database`, `/preprocessing`, and the frame-upload route.

**Depends on:** FastAPI, shared response envelopes, module request/response
schemas, and `core` dependencies. It must not own domain processing.

## `image_receiver`

**Role:** Boundary for receiving image/frame references.

**Inputs:** `UploadFrameRequest` containing a source/reference and optional
frame metadata.

**Outputs:** `UploadFrameResponse`, representing accepted frame metadata.

**Depends on:** shared `Frame` vocabulary, Pydantic, and API response
envelopes. It provides input to preprocessing; storage and capture mechanisms
are future dependencies.

## `preprocessing`

**Role:** Image enhancement and deterministic text normalization.

**Inputs:** Raw frame arrays / BGR images, and recognized raw OCR token lists.

**Outputs:** Enhanced BGR frame for OCR/Gesture, and normalized reading text via idempotent transformation rules (`normalize_reading_text`).

**Depends on:** OpenCV, NumPy, and narrow regex patterns. Preserves raw token indices and coordinates without mutation.

## `ocr`

**Role:** OCR extraction, spatial drop-cap detection, and dual-path token modeling.

**Inputs:** `ProcessedImage` / JPEG bytes via `Google Vision` (or `OCR.Space` prototype).

**Outputs:** `RawOCRToken` (immutable bounding boxes for gesture mapping) and `NormalizedToken` / `ReconstructedReadingPage` for semantic reading, OLED text, and TTS queues.

**Depends on:** Google Vision API, cached vision responses, and `PageGeometryValidator` for spatial consistency.

## `merge_memory`

**Role:** Multi-frame page accumulation, same-page overlap detection, and immutable page history.

**Inputs:** Polled OCR text frames with line bounding boxes and page indices (`apply_frame`).

**Outputs:** Consolidated `Page` objects, monotonic `source_version`, and structured `ContentMap` with reading pointers.

**Depends on:** `GroqReconstructor` (via `GROQ_API_KEY_2`) or deterministic offline line-stitching fallback.

## `reading_engine`

**Role:** Reading-session lifecycle, runtime coordination, and device control loop.

**Inputs:** `DeviceLoop` hardware ticks, `SessionEvent` triggers (`READING_UPDATE_REQUESTED`, `MEANING_MODE_ON/OFF`), and reading pointer advancements.

**Outputs:** Real-time `ReadingSessionState`, `MeaningLookupResult` for OLED display, and finalized `SessionAnalytics`.

**Depends on:** `MergeMemory`, `ReadingSpeedService`, `FocusAnalyticsEngine`, and `PlaybackEngine`.

## `gesture_engine`

**Role:** Real-time fingertip localization, multi-frame consensus, and word selection.

**Inputs:** Camera frames (BGR array) and active `PageContext` / `FrameContext`.

**Outputs:** `FingerObservation`, `SelectionResult` with bounding box containment scores, and `GestureTransaction` lifecycle states.

**Depends on:** MediaPipe Hands (Tier 1) and OpenCV adaptive contour fallback (Tier 2).

## `ai_engine`

**Role:** Meaning Mode dictionary explanations and end-of-session review generation.

**Inputs:** Target word, sentence context, and reader lookup history.

**Outputs:** Real-time OLED definition string ($18\text{ chars/line}$), TTS audio text, and JSON review payload (quizzes & flashcards).

**Depends on:** Groq API (via `GROQ_API_KEY_1`, `fast_model`, and `chat_model`).

## `audio_engine`

**Role:** Dual-layer audio pipeline (neural TTS narration + ambient atmospheric soundscapes).

**Inputs:** Sentence spans from `ReadingEngine`, scene triggers from `SceneController`, and reader voice preferences.

**Outputs:** Streamed speech audio (`EdgeTTSProvider` / `LocalAudioSink`), ambient audio playback (`LocalAmbientProvider`), and automatic audio ducking on Meaning Mode activation.

**Depends on:** `edge-tts`, `pygame.mixer`, and local ambient MP3 assets (`assets/audio/`).

## `database`

**Role:** Persistence schema boundary.

**Inputs:** Domain records mapped to SQLAlchemy models.

**Outputs:** Relational table records for sessions, OCR results, selected words,
AI responses, flashcards, quizzes, and reading statistics.

**Depends on:** SQLAlchemy 2.x and shared domain concepts. The module currently
contains declarative metadata and models only; engines, sessions, repositories,
migrations, and APIs are outside its scope.

## `utils`

**Role:** Small cross-cutting utilities that do not own domain state.

**Inputs/outputs:** Utility-specific values, with no domain workflow implied.

**Depends on:** Python standard library or narrowly scoped shared utilities.
Utilities should remain dependency-light and avoid importing feature modules.

## `core` and `config`

**Role:** Runtime configuration, environment access, dependency wiring, and
logging boundaries.

**Inputs:** Environment variables and application lifecycle context.

**Outputs:** Settings objects, shared dependencies, and configured loggers.

**Depends on:** Pydantic Settings, dotenv support, and Python logging. These
layers must not contain feature behavior.

## `frontend`, `assets`, `logs`, `tests`

These are delivery and project-support boundaries. `frontend` consumes API
outputs; `assets` stores project resources; `logs` stores runtime output; and
`tests` verifies contracts and module boundaries. None is a backend domain
dependency.
