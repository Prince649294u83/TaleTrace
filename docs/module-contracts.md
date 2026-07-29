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

**Role:** Boundary for image preparation before OCR.

**Inputs:** A frame reference and preparation options defined by a future
preprocessing contract.

**Outputs:** A provider-neutral prepared-image reference consumable by OCR.

**Depends on:** image/frame contracts and the future image-processing provider.
No preprocessing behavior is specified here.

## `ocr`

**Role:** OCR boundary that normalizes recognized content.

**Inputs:** `OcrProcessRequest`, containing `ProcessedImage` and the fixed
`DOCUMENT_TEXT_DETECTION` operation contract.

**Outputs:** `OcrProcessResponse` containing a status and `OCRPage` values;
pages contain paragraphs, words, confidence, and optional bounding boxes.

**Depends on:** prepared-image contracts, shared OCR domain models, and
`OcrParserInterface`/`OcrProcessorInterface`. A future provider may be placed
behind these interfaces.

## `reading_engine`

**Role:** Reading-session lifecycle and current reading-context boundary.

**Inputs:** Session ID, optional timestamps, page number, `OCRPage`, or selected
word text, as defined by `ReadingEngineInterface`.

**Outputs:** `Session`, `ReadingState`, current `OCRPage`, paragraph text,
session summary, or no value for recording operations.

**Depends on:** shared session, reading-state, and OCR contracts. It may later
depend on database persistence but does not require a concrete store at the
interface boundary.

## `gesture_engine`

**Role:** Hand-gesture and OCR-selection boundary.

**Inputs:** `GestureDetectionRequest` with a shared `Frame`; or
`OcrMappingRequest` with a normalized `FingerPoint` and OCR pages.

**Outputs:** `GesturePlaceholderResponse` with operation and pending status,
optionally carrying a `Gesture` or selected `OCRWord` once implemented.

**Depends on:** shared frame, gesture, and OCR contracts. MediaPipe Hands is a
reserved future provider dependency and is not part of the current contract.

## `ai_engine`

**Role:** Provider-neutral boundary for reader assistance capabilities.

**Inputs:** `AiInput` containing optional text, content reference, and string
metadata.

**Outputs:** `AiPlaceholderResponse` with status, capability name, and a
placeholder message. The existing explanation route uses the compatible
`AiExplainRequest` and `AiExplainResponse` types.

**Depends on:** Pydantic and shared content references. Interfaces cover Prompt
Builder, Explanation Engine, Adaptive Reading, Novel Mode, Image Decision,
Summary Generator, Flashcard Generator, and Quiz Generator. No LLM or provider
SDK is a current dependency.

## `audio_engine`

**Role:** Boundary for future text-to-speech and playback delivery.

**Inputs:** Future text, voice, and playback request contracts.

**Outputs:** Future audio reference and playback status contracts.

**Depends on:** reading/AI text contracts, API envelopes, and a future audio
provider. No audio provider behavior is specified here.

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
