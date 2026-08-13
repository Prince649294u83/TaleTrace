# AI Engine contracts

The AI Engine is a provider-neutral boundary. It exposes four implemented
capabilities (Groq-backed) plus placeholder shells for capabilities that are not
built yet. No engine is tied to a specific book — book identity arrives as
metadata on every request.

## Input

Every capability accepts an `AiInput` (or a subclass). The preferred field is
`context`, a `ReadingContext` supplied by the Reading Engine:

| Field | Meaning |
| --- | --- |
| `book` | `BookMetadata`: `title`, `author`, `genre`, `chapter`, `audience` |
| `page_number` | Current page |
| `previous_paragraph` | Paragraph before the current one |
| `current_paragraph` | Paragraph the reader is on |
| `selected_word` | Word the reader selected |
| `mode` | `standard`, `adaptive`, `disability`, `novel`, `study`, `exam` |
| `previously_explained` | Words already explained this session (session memory) |

The older flat fields (`text`, `metadata["word"]`, `metadata["mode"]`,
`metadata["book_title"]`, …) still work. `AiInput.resolved_context()` merges
them into a `ReadingContext`, with `context` taking precedence.

Session memory is passed in per request; the Reading Engine owns persistence
across requests.

## Output

Implemented capabilities return an `AiCapabilityResponse`:

- `status` — `ok` or `error`
- `capability` — which engine answered
- `data` — the parsed payload (read this)
- `message` — the same payload as a JSON string, kept for earlier callers

### `ExplanationEngine.explain` → `explanation_engine`

```json
{"oled_text": "...", "full_explanation": "...", "difficulty_level": "beginner|intermediate|advanced"}
```

### `ImageDecision.decide` → `image_decision`

```json
{"show_image": true, "image_query": "...", "image_type": "illustration|diagram|photo|map|portrait|none", "reason": "..."}
```

`image_type` is `none` whenever `show_image` is false, so the retrieval layer
knows what style of image to fetch from a free image source.

### `NovelMode.create` → `novel_mode`

```json
{"scene_mood": "...", "emotion": "...", "intensity": 0.84, "audio_tag": "...", "companion_commentary": "..."}
```

Scene moods are genre-neutral: `peaceful`, `wonder`, `musical`, `suspense`,
`action`, `comedy`, `sorrow`, `neutral_narration`. `intensity` is clamped to
0.0–1.0 so the Audio Engine can crossfade instead of switching abruptly.

### `SummaryGenerator.summarize` → `summary_generator`

```json
{"flashcards": [...], "quiz": [...], "words_learned": [{"word": "...", "takeaway": "..."}], "session_summary": "..."}
```

`words_learned` feeds vocabulary-growth analytics; it falls back to the distinct
words in `session_history` if the model omits it.

## Prompt construction

`PromptBuilder` (`prompts.py`) builds every system prompt. Shared JSON rules,
persona, book description, per-mode guidance, and session memory live there
only. Engines never assemble prompt text inline.

Responses use Groq's JSON mode (`response_format={"type": "json_object"}`) with
a bounded retry. Migrating to a `json_schema` response format would give
stronger guarantees once the schemas are final.

## Configuration

`GROQ_API_KEY_1` is required at call time — the AI Engine's own key, not the
Merge Engine's `GROQ_API_KEY_2` (see `shared/groq_keys.py` for why they are
separate). `GROQ_MODEL` overrides the default model. The client is created
lazily and belongs to this module alone, so the app still boots without a key
and no other subsystem can exhaust this one's rate limit.

## Placeholders

`PromptBuilderInterface`, `AdaptiveReadingInterface`,
`FlashcardGeneratorInterface`, and `QuizGeneratorInterface` remain contracts
only. The corresponding `*Placeholder` classes return a stable `pending`
response identifying the capability.
