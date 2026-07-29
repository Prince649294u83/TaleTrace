# TaleTrace system architecture

## Boundary map

TaleTrace is organized as a FastAPI delivery layer around independent domain
modules. Shared contracts live in `backend/app/models`; cross-cutting concerns
live in `backend/app/core`, `backend/app/config`, and `backend/app/utils`.

```text
frontend / external clients
            |
         API routers
            |
  image -> preprocessing -> OCR -> reading context
                                      |       |
                              gesture engine  AI engine
                                      |       |
                                  audio engine
                                      |
                                  database
```

The diagram is a boundary map, not an implementation sequence. Modules should
depend on shared contracts and interfaces rather than concrete providers.

## Dependency direction

- `api` owns HTTP composition and response envelopes.
- `image_receiver` owns frame-ingestion contracts.
- `preprocessing` owns the boundary for prepared image data.
- `ocr` consumes prepared-image contracts and emits normalized OCR contracts.
- `reading_engine` coordinates session state and reading context contracts.
- `gesture_engine` consumes frame/OCR contracts and emits gesture/selection contracts.
- `ai_engine` consumes content context and emits capability-specific response contracts.
- `audio_engine` consumes future text/audio contracts and emits playback contracts.
- `database` owns persistence schemas only.
- `core`, `config`, and `utils` provide shared infrastructure contracts.

Concrete providers must remain behind the relevant module interface. No module
should import a provider-specific SDK into shared domain contracts.

## Shared vocabulary

Shared Pydantic contracts are in `backend/app/models/domain.py` and include
`Frame`, `OCRPage`, `OCRParagraph`, `OCRWord`, `Gesture`, `ReadingState`, and
`Session`. HTTP responses use `ResponseEnvelope` from
`backend/app/models/responses.py`.
