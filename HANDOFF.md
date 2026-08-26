# Handoff — Phase 1: Quiz + Flashcards

**Status: code complete, uncommitted, untested.** Written 2026-08-26. Last commit
is `2d47984`. Everything below is in the working tree, not in git.

Read [AGENTS.md](AGENTS.md) first for the project as a whole and the rules that
cannot be broken. This file is only about the in-flight change, and it should be
deleted once the work in "What is left" is done and pushed.

---

## 1. What Phase 1 is

One integration milestone: the Quizzes and Flashcards pages stop using the
browser mock and start serving what the AI Engine already wrote.

```
completed session → AiBridge.review() → sessions.review_payload
                  → review.py (parse → validate → merge → cap) → the page
```

**The AI Engine must not be called again because somebody opened a page.** A
second call would spend a request to produce a *different* quiz about the same
reading, and the reader would have no way to tell why the questions changed
between two clicks. `review_payload` is the source of truth. There is no
`groq` import in the new code and none may be added.

---

## 2. What is already written (working tree)

| File | State | What it is |
|---|---|---|
| `backend/app/modules/database/review.py` | **new**, 355 lines | the whole merge layer |
| `backend/app/api/companion.py` | +136 lines | 2 schemas, `_selected`, `NO_REVIEW`, 3 routes |
| `frontend/src/services/api.js` | 3 exports rewritten | the three calls now `fetch()` |
| `frontend/src/services/mockBackend.js` | 481 → 374 lines | quiz/flashcard banks deleted |

`git diff HEAD --stat` should show exactly those three modified files plus the
one untracked `review.py`. Nothing else. `review.py` is untracked — do not lose
it to a `git clean`.

### `review.py`, the parts worth knowing

* `flashcards_in(row)` / `quiz_in(row)` — one session's cards and questions.
* `merge_flashcards(rows)` / `merge_quiz(rows, limit=MAX_QUIZ_QUESTIONS)` —
  several sessions.
* `quiz_id(ids)` / `sessions_in(quiz_id)` — quiz identity.
* `score(questions, answers)` — server-side marking.

Four decisions that are load-bearing:

1. **The quiz id *is* the answer-key storage.** `quiz_id` is the sorted,
   de-duplicated session ids joined with `.` (session ids are `str(uuid4())`, so
   they contain hyphens but never a dot). `merge_quiz` is deterministic, so
   `POST /quiz/submit` re-derives the identical quiz — and therefore the key —
   from the ids inside the id it was handed. No `quizzes` table, no in-memory
   map. It survives `--reload`, cannot leak, has nothing to evict, and Phase 2
   can still drop the legacy `quizzes` table without touching this. If you
   change `merge_quiz`'s ordering, dedup or cap, **every quiz open in a browser
   at that moment scores against different questions.**
2. **A question that cannot be marked is dropped, not guessed.** The payload's
   `correct_answer` is a *string*; it is resolved to an index at parse time. An
   answer matching zero or more than one option drops the question — never
   scored as index 0. Two identical options drop the question too (removing the
   duplicate would change what the reader is being asked).
3. **Round-robin merge.** Each session contributes its first question, then its
   second, and so on, so a 10-question cap over five sessions is not filled
   entirely from the oldest one.
4. **The quiz is capped at 10; the deck is not capped at all.** A quiz is a
   sitting with an end. A deck is a reference, and every card in it is a word the
   reader stopped reading to ask about — dropping some to hit a round number is
   the same mistake as a `words_read` threshold in the analytics.

Dedup is by `_key()`: casefold, collapse inner whitespace, strip edge
punctuation. So `Chlorophyll`, `chlorophyll` and `chlorophyll,` are one word, and
a question asked with and without its `?` is one question.

### The three routes (`/api` prefix)

| Route | Returns | Errors |
|---|---|---|
| `POST /quiz` `{sessionIds}` | `{quizId, questions:[{id,question,options}]}` — **no `correctIndex`** | 422 empty selection, 404 unknown id, 404 `NO_REVIEW` |
| `POST /quiz/submit` `{quizId, answers:{qid:index}}` | `{score,total,percent,results:[{questionId,correct,correctIndex}]}` | 404 if the sessions are gone |
| `POST /flashcards` `{sessionIds}` | `{cards:[{id,term,definition}]}` | same as `/quiz` |

`total` is the number of questions *asked*, so an unanswered question is wrong.
An answer for an unknown question id earns nothing rather than being a 400 — a
stale tab resubmitting already gets the right score.

The frontend contract in `api.js` did not change shape: no page or component
under `frontend/src/pages` or `components/quiz|flashcards` was edited, and none
needs to be.

---

## 3. What is left, in order

### 3a. Backend tests — `tests/test_companion_api.py`

Use the existing `client` / `db_factory` fixtures and the `analytics_with` and
`store` helpers at the top of that file; pass `review_payload` through whatever
`store` forwards to `record_session`. Cases, one per mistake that would be
invisible on screen:

- a `correct_answer` absent from `options` is dropped, **not** scored as index 0
- two identical options drop the question
- a flashcard with no usable definition is dropped; one with only
  `hint_from_story`, and one with only a `words_learned` takeaway, are kept
- a session with `words_learned` but no `flashcards` list still deals cards
- the same word across two sessions yields one card (`Chlorophyll` vs
  `chlorophyll,`)
- the 10-question cap holds, and with two 8-question sessions the ten come from
  **both** (round-robin, not the first session twice)
- the generate response contains no `correctIndex` anywhere
- `POST /quiz/submit` scores correctly **without `POST /quiz` having been called
  in the same test** — that is the proof the id re-derives the key
- unanswered → wrong; unknown question id → ignored; `total` = questions asked
- `sessionIds: []` → 422 "Select at least one session first."
- an unknown session id → 404
- sessions whose `review_payload` is `{}` → 404 carrying `NO_REVIEW`
- a `quizId` whose sessions were deleted → 404, not a partial score
- flashcards are uncapped: 15 distinct words over several sessions all appear

### 3b. Frontend test — `frontend/src/services/api.quiz.test.js`

Mirror `api.analysis.test.js` (vitest, `fetch` stubbed). Assert: the three calls
hit `/api/quiz`, `/api/quiz/submit`, `/api/flashcards` with the right bodies; a
non-2xx propagates the server's sentence with **no mock fallback**; and
`mockBackend` exports nothing matching `/quiz|flashcard/i`.

### 3c. Real review payloads in the seeded corpus

**This is the one open design question.** `scripts/seed_history.py` deliberately
leaves `review_payload` empty — its docstring says inventing plausible AI content
is exactly what the corpus exists to replace, and `row.summary` says so on every
seeded row. So today `python scripts/seed_history.py` gives you charts and
sessions but **every** quiz/flashcard request 404s with `NO_REVIEW`.

The user's direction: *"Use the seeded corpus as the deterministic baseline and
then add the real review payloads required for the learning flows."* So the seeded
rows need payloads. Suggested resolution, cheapest rung first: give a small number
of seeded sessions a payload built from a hand-written constant in
`seed_history.py`, marked with the existing `SEED_MARKER` in the summary the way
the rest of the script marks itself, and say in the docstring that these are
fixtures for the Quizzes and Flashcards pages, not AI output. Do not fabricate
per-word "definitions" for words no reader looked up — reuse the words the seeded
sessions already claim as lookups. Alternatively run `scripts/golden_session.py`
for a row with genuine Groq content and leave the rest empty; that costs a real
API call and is not deterministic across machines, which is why it is second.

### 3d. Documentation that is now false

Three places still say quizzes and flashcards are mocked. Fix after the tests
pass, not before:

- `AGENTS.md` §6 (real-vs-mocked table) and §7 (roadmap — Phase 1 becomes done)
- `README.md` §2.4
- `frontend/README.md` — the table under "What is real and what is still mocked",
  the two paragraphs after it (the one beginning "Quizzes and flashcards use
  sample banks" is now wrong end to end), the `services/` line in the project
  structure, and the Notes bullet claiming "quiz scoring is done in the mock".

### 3e. Ship it

```bash
python -m pytest                 # was 703 passed before this change
cd frontend && npm test          # was 7 passed
```

Then commit **everything except `.env`** and push to `origin/Latest-changes`.
Confirm with `git status --short` before staging: two `.env` files hold live
group-project keys (repo root and `backend/app/OCRandGESTURE/.env`). Report
secrets as present/absent, never by value.

---

## 4. Traps specific to this change

- **`review_payload` may be `{}`, may be missing keys, may hold a string where a
  list belongs.** `_items()` already tolerates all three. Keep it that way — the
  frontend is the one place in this system that must never have to interpret an
  inconsistent payload.
- **Dropping is silent on purpose.** A reader cannot act on "one of your six
  questions was malformed" and cannot edit the payload, so there is no message.
  The count on screen is the count of questions that work.
- **Do not add a `words_read`-style threshold** to hide sessions with thin
  payloads. Same rule as the analytics.
- `session_wpm == 0.0` and `DifficultyLevel.UNKNOWN` are sentinels meaning *not
  measurable* / *not rated*. Nothing in this change touches them; don't let a new
  test assert them as numbers.
- The `(462,0)` gesture false positive is **deliberately unfixed** — the user
  said to leave the gesture path alone.

---

## 5. After Phase 1

The order the user set: **DB schema cleanup** (drop the unused legacy tables
`ocr_results`, `selected_words`, `ai_responses`, `flashcards`, `quizzes`,
`reading_statistics`) → **authentication** and server-side reader ownership →
**remove the remaining mocks** (`login`/`signup`, plus the now-dead session
helpers still sitting in `mockBackend.js`) → **ESP32 hardware validation** →
**final demo / E2E rehearsal**.

The runtime core is closed. Phase 1 and everything after it is integration work;
`backend/app/OCRandGESTURE/` stays frozen and the reading pipeline stays as it is.
