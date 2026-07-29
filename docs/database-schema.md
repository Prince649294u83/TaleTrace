# Database schema vocabulary

The database module exposes SQLAlchemy 2.x models only. It does not expose
database APIs or connection management.

| Table | Primary record | Inputs represented | Outputs represented |
|---|---|---|---|
| `sessions` | Reading session | status, source reference, lifecycle timestamps | session identity and ownership root |
| `ocr_results` | OCR capture | session, source, extracted text, structured content, confidence | normalized OCR persistence record |
| `selected_words` | Gesture-selected word | session, optional OCR result, text, bounding box, confidence | selected-word history |
| `ai_responses` | Capability response | session, capability, prompt reference, metadata | response text and response metadata |
| `flashcards` | Study card | session, front, back, source reference | persisted card content |
| `quizzes` | Quiz | session, title, structured questions, score | persisted quiz content and result |
| `reading_statistics` | Reading metrics | session, words, duration, pages, selections, additional metrics | session reading statistics |

All feature records reference `sessions`. Session ownership is represented by
relationships and cascading deletion. Structured fields use JSON so the schema
can preserve provider-neutral payloads without defining provider internals.
