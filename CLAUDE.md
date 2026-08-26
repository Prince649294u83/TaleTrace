# CLAUDE.md

**Read [AGENTS.md](AGENTS.md) before your first edit.** It is the handoff
document: what this project is, the invariants that look like bugs, the commands,
what to do next, and the traps that have already cost hours.

The rules below are repeated here because they are the expensive ones to get
wrong, and this file is the one that loads automatically.

- **Never commit `.env`.** Two of them hold live API keys (repo root and
  `backend/app/OCRandGESTURE/.env`). Group project. Report secrets as
  present/absent, never by value.
- **`GROQ_API_KEY_1` is the AI Engine's. `GROQ_API_KEY_2` is Merge Memory's.**
  Separate by design. Never share a client, a config field, or a factory
  between them.
- **`backend/app/OCRandGESTURE/` is a frozen spec.** Read it, never edit it.
  Where migrated code and the prototype disagree, the prototype is right.
- **Google Vision is the only production OCR engine.** The replay path is
  test-only.
- **The module is "Reading Focus Analysis"**, never "Distraction Detection".
  Idle time is worded "Possible Idle Time".
- **The website reviews reading; it never performs it.** No "start reading"
  button in the browser, ever.
- **Push to `origin/Latest-changes`.** "Latest" means that shared branch.
- **`session_wpm == 0.0` and `DifficultyLevel.UNKNOWN` are sentinels**, meaning
  *not measurable* and *not rated*. They serialise to `null` and are excluded
  from means. Never render either as a number.
- **No `words_read` threshold, anywhere.** Hiding short sessions would discard
  legitimate reading.

Run it: `python -m backend.app.live_session --check` (hardware), then
`python scripts/dev.py` (both servers). Tests: `python -m pytest` → 703 passed;
`cd frontend && npm test` → 7 passed.
