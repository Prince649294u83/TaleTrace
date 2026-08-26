# AGENTS.md — handoff for the next AI agent

You are picking up a working system, not a greenfield. Read this file top to
bottom before your first edit. It exists because several things in this
codebase look like bugs and are not, and because a handful of rules here were
paid for in wasted hours.

Order of reading: this file → [README.md](README.md) §2 (how to run it) →
[docs/architecture.md](docs/architecture.md) if you need module boundaries.

---

## 1. What TaleTrace is

A physical smart bookmark. An ESP32-CAM clipped to a paper book watches the
page, works out which line the reader's finger is on, reads that line aloud,
and — when the reader presses a button on a word — explains it. Everything the
reader does is recorded, and a companion website reviews it afterwards.

Two halves, and they are not symmetrical:

- **The device pipeline** (`backend/app/`) is where the intelligence is. It runs
  from a terminal against real hardware.
- **The website** (`frontend/`) is a **review app, not a reading app**. There is
  no "start reading" button anywhere and there must never be one. Reading
  happens on the rig; the site shows what the rig produced.

If a request seems to ask for reading in the browser, it is a misread of the
product. Check before building it.

---

## 2. Rules you cannot break

These are the user's standing instructions, not suggestions. Breaking one is
worse than shipping nothing.

**Never commit `.env`.** This is a group project and two `.env` files hold live
API keys: `.env` at the repo root and `backend/app/OCRandGESTURE/.env`. Do not
read them, do not print their contents, do not `git add` them. Report secrets
as present/absent booleans only. `git add -A` is safe — `.gitignore` covers
them — but check `git status` before every commit anyway.

**Two Groq keys, never shared.** `GROQ_API_KEY_1` is for the AI Engine only
(Meaning Mode, contextual explanation). `GROQ_API_KEY_2` is for Dynamic
Merge / text formatting only. They are separate by design so that one
feature's rate limit cannot starve the other, and so a leaked key has a bounded
blast radius. Do not "simplify" them into one client, one config field, or one
shared factory. If you find yourself writing `GROQ_API_KEY` with no suffix, stop.

**`backend/app/OCRandGESTURE/` is a frozen specification.** It is the original
working prototype and it is the functional spec for the migrated modules. Read
it; never edit it. When migrated behaviour and prototype behaviour disagree, the
prototype is right — this is a behaviour-preserving migration, not an
improvement project. (Its `Gesture/` subdirectory is a separate git repo and is
gitignored on purpose; staging it records a useless gitlink.)

**Google Vision is the only production OCR engine.** The JSON replay path is a
test-only adapter that replays cached Vision responses so tests need no API
call. Never present replay as an OCR engine or add a second production engine.

**The module is called Reading Focus Analysis.** Never "Distraction Detection",
in code, comments, docs, or UI. Idle time is worded "Possible Idle Time" and
"reading paused longer than expected" — never anything that accuses the reader
of not paying attention. The system reports what it saw; it does not judge.

**Greyscale photographs are unusable.** Most images on this device are greyscale
and the pipeline needs colour. When you need a test page, use a colour
photograph or generate one with `scripts/synthetic_page.py`.

**Push target is `origin/Latest-changes`.** When the user says "push to
Latest", that is this shared working branch. Do not create a new branch and do
not push to `main`.

**Do not touch the core pipeline without a reason.** OCR → Gesture → Merge
Memory → Reading Pointer → TTS → Meaning Mode → AI Engine → Reading Speed is
finished and verified. Touch it only if a new regression appears or a task
explicitly requires consuming an already-persisted field.

---

## 3. The pipeline, and where each stage lives

```
ESP32-CAM frame
  → image_receiver/     accepts the frame
  → preprocessing/      deskew, enhance (its output is what the Vision cache is keyed on)
  → ocr/                Google Vision → words with bounding boxes
  → gesture_engine/     fingertip → the word being pointed at
  → merge_memory/       stitches pages, detects same-page, syncs the pointer   [GROQ_API_KEY_2]
  → reading_engine/     the session: pointer, lookups, finish_session()
  → audio_engine/       TTS narration
  → ai_engine/          Meaning Mode + end-of-session review                   [GROQ_API_KEY_1]
  → reading_speed/      baseline, session WPM, per-page difficulty
  → focus_analytics/    Reading Focus Analysis
  → database/           one row per finished session
```

Entry points:

| File | What it is |
|---|---|
| `backend/app/live_session.py` | the real thing, with hardware. `--check` is preflight. |
| `backend/app/simulated_session.py` | the same run with no hardware |
| `backend/app/main.py` | FastAPI app: device API + companion API + CORS |
| `backend/app/api/companion.py` | every `/api/*` route the website calls |
| `backend/app/modules/database/analysis.py` | the Analysis aggregation |
| `frontend/src/services/api.js` | the **only** file in the frontend that talks to the backend |

`docs/module-contracts.md` has inputs/outputs per module.

---

## 4. Invariants that look like bugs

Each of these has been "fixed" by mistake at least once. Do not undo them.

**`session_wpm == 0.0` is a sentinel, not a speed.** A session shorter than
`MIN_MEASURABLE_READING_MS` (1000 ms) cannot be timed, so its pace is recorded
as `0.0` meaning *not measurable* — never "read at zero words per minute". It is
excluded from every daily mean. A day where nothing was measurable reports
`wpm: null`, and the chart draws a gap. Rendering it as `0` would invent a
measurement.

**`DifficultyLevel.UNKNOWN` is a refusal to rate.** It serialises to `null`,
never to "Medium". The entire point of `UNKNOWN` is to avoid claiming a page was
fine when nothing was assessed. Excluded from difficulty means too.

**Three different word counts, three different meanings.** Do not merge them:

| Field | Means |
|---|---|
| `words_read` | reading progress — how far the reader got |
| `words_spoken` | narration — what TTS said out loud |
| `reading_time_ms` | narration duration, not reading duration |
| `tts_assisted` | metadata: was narration on |

Reading speed uses `words_read` and `session_wpm`. It must never be
recomputed from TTS data — narration and reading are different events.

**SQLite has no timestamp type.** A `DateTime(timezone=True)` column written
from an aware-UTC datetime comes back **naive**. Everything goes through
`as_utc`. To backdate a row you must write
`.astimezone(timezone.utc).replace(tzinfo=None)` — anything else silently
shifts the day a session belongs to.

**Days are calendar days at the reader's local midnight**, not rolling 24-hour
windows. The frontend formats dates because the browser is the only part of the
system that knows the reader's timezone. (The deleted mock used rolling windows
and capped All Time at 90 days; the real API does neither.)

**Difficulty means use `_round_half_up`.** Python's `round()` is banker's
rounding — `round(2.5) == 2`. Every tie in TaleTrace breaks toward the harder
read, because telling a reader a page was easier than it was is the more
damaging error.

**`SessionAnalytics` is a Pydantic model.** Use `.model_fields`;
`dataclasses.fields()` raises.

**No `words_read` threshold exists anywhere, and none may be added.** It is
tempting to hide short sessions from Analysis. The user forbade it explicitly:
a legitimate short reading session would be silently discarded. Data hygiene is
solved by provenance — clean the database, re-run known-good scenarios — never
by a filter in the aggregation. `test_a_very_short_session_still_appears` stores
a one-word session and asserts it is present.

**Analysis has no fallback.** If `/api/analysis` fails, the page shows its error
state. The randomised `Math.random()` mock aggregation is *deleted*, not merely
unreferenced, and `frontend/src/services/api.analysis.test.js` asserts
`mockBackend` exports nothing matching `/analysis/i`. Invented figures that
render identically to measured ones are worse than an error, because nobody can
tell by looking which they are seeing.

---

## 5. Commands

Everything runs from the repo root.

```bash
# hardware — run this FIRST, before any real session
python -m backend.app.live_session --check

# a reading session
python -m backend.app.live_session          # with the rig
python -m backend.app.simulated_session     # without it

# the website: backend + frontend, one Ctrl-C stops both
python scripts/dev.py                       # then open the Local: URL Vite prints

# tests
python -m pytest                            # 720 passed
cd frontend && npm test                     # 14 passed (vitest)
python scripts/verify_all.py                # the end-to-end gate

# the demo corpus in taletrace.db
python scripts/seed_history.py              # top up
python scripts/seed_history.py --reset      # rebuild from scratch
```

Demo login: `demo@taletrace.app` / `demo1234`. The **account** is local
(localStorage); the **history** it shows is real, from the `sessions` table.

`taletrace.db` **is committed on purpose** — it is the seeded demo corpus, not
anybody's private reading, and having the same twelve sessions on every clone is
what makes Analysis show the same charts for everyone without a rig. It is a
binary, so git cannot merge it: resolve a conflict by taking either side and
re-running `seed_history.py --reset`, never by hand.

---

## 6. Real vs mocked, right now

| Real, from the backend | Still `mockBackend.js` |
|---|---|
| reader profile and settings | accounts: signup / login / logout |
| device status (a live probe of the rig) | |
| dashboard | |
| **all five Analysis charts** | |
| sessions, folders, session details | |
| reading-speed test and baseline | |
| **quizzes: generation, scoring, merge** | |
| **flashcards: generation, dedup, merge** | |

Quizzes and flashcards are served from persisted `review_payload` blobs that
the AI Engine wrote when each session ended. "Generate Quiz" merges rows; it
never causes a new AI call. The answer key stays server-side — the browser
holds only a `quizId` that lets the server rebuild it at submit time.

A subset of the seeded sessions (the Biology and English ones) carry
handcrafted `review_payload` fixtures so the Quiz and Flashcards pages work
against the demo corpus without a Groq key. Sessions without lookups return
a 404 with a human-readable explanation.

Accounts are in `localStorage` because there is no server-side authentication
yet. No password reaches the server. Nothing here may be exposed to a network
until that changes.

---

## 7. What to do next

The user's ordering. Do not reorder without asking.

1. ~~**Quizzes and Flashcards from persisted `review_payload`**~~ — **done.**
   Merge endpoint over existing rows, deterministic seed fixtures, full test
   coverage (backend 720 tests, frontend 14 tests), no new AI call.
2. **Database schema cleanup** — six legacy tables are dead and unwritten:
   `ocr_results`, `selected_words`, `ai_responses`, `flashcards`, `quizzes`,
   `reading_statistics`. `review_payload` is the canonical store; drop them.
3. **Authentication** — real server-side accounts and reader ownership. Until
   this lands, the server holds exactly one reader (`local-reader`) and `api.js`
   drops the `userId` argument on the wire; when auth arrives that argument
   becomes a session cookie with no call-site changes.
4. **Remove the remaining mocks** feature by feature: `login`/`signup`.
   (`generateQuiz`, `submitQuiz`, `generateFlashcards` are already real.)
5. **Hardware validation** — ESP32-CAM, both buttons, the device loop, on the
   real rig.
6. **Final demo / end-to-end rehearsal.**

Deliberately not built, and not oversights: a page for `FocusReport` (the
backend's richest output — per-paragraph revision priority, evidence, Possible
Idle Time — currently reaches the site only as SessionDetails' single difficulty
word), and Alembic (one file, `create_all` is enough).

---

## 8. Traps that have already cost hours

**Port 8000 already in use.** uvicorn's Windows message is `[WinError 10013] An
attempt was made to access a socket in a way forbidden by its access
permissions`, which reads like a firewall problem and is not one — it is a
backend left over from an earlier run. `scripts/dev.py` now checks the port
first and says so in plain words. To clear it:
`netstat -ano | findstr :8000` then `taskkill /F /PID <pid>`.

**Git Bash mangles `/F` into `F:/`.** MSYS path conversion. Double the slashes
when calling Windows CLI tools from Git Bash: `taskkill //F //T //PID 1234`.
Calls made through `subprocess` with an argument list and no shell are unaffected.

**The Windows console is cp1252.** An em dash in a *printed* string renders as
`?`. Keep typography in docstrings and comments; use ASCII in `print()`.

**Never hardcode 5173.** Vite takes the next free port when 5173 is busy and its
`/api` proxy follows it. A banner insisting on 5173 sends the reader to whatever
else is on that port. Print the backend URL and defer to the `Local:` line Vite
prints.

**The Vite proxy must target `127.0.0.1`, not `localhost`.** On Windows
`localhost` can resolve to IPv6 `::1` while uvicorn is listening on IPv4 only.

**mediapipe must stay pinned.** Gesture tier 1 dies *silently* above
mediapipe 0.10.21 — no exception, just no landmarks. numpy and opencv are capped
alongside it. Do not "update dependencies" here without running the gesture
tests on real frames.

**`npm` is `npm.cmd` on Windows.** `subprocess` will not find it by the bare
name without a shell; resolve it with `shutil.which("npm")`. And `terminate()`
kills only the `.cmd` shim, leaving node holding the port — use
`taskkill /T /F /PID`.

**recharts renders nothing in jsdom.** It measures its container and jsdom
reports 0×0, so a render test asserts against an empty SVG. Test the `api.js`
adapter instead — it is the last place the values are readable and the only
place a mock could sneak back in. (`vitest`, not `node --test`: plain Node
cannot resolve `import.meta.env`, which `api.js` depends on.)

**`git push` fails with "Password authentication is not supported."** Git
Credential Manager needs to be allowed to prompt:
`git -c credential.interactive=always push origin Latest-changes`.

---

## 9. Known issues, deliberately open

**A gesture false positive at `(462,0)`.** A fingertip is occasionally reported
at the top-left of the frame. The user's instruction was explicit: *"For now
don't change anything in gesture part go to next step."* Leave it.

**Step 8 of the verification script is blocked** on a fresh colour photograph
of a hand pointing at a different word. Not a code problem.

**`scripts/verify_all.py` line 70** was flagged as a syntax error by an external
parser (graphify). Never confirmed; the file runs and the suite passes, so it is
almost certainly a limitation of that parser rather than a real defect.

---

## 10. How to work here

The user runs **Ponytail mode**: climb the ladder and stop at the first rung
that holds. Deletion over addition. Boring over clever. A bug fix means the root
cause with every caller grepped, not a guard at the reported call site. Never
simplify away input validation at a trust boundary, error handling that prevents
data loss, security, or accessibility. Hardware gets a calibration knob, because
a real sensor is never the ideal on paper. Non-trivial logic leaves exactly one
runnable check behind. Mark a deliberate corner-cut with a `ponytail:` comment
naming the ceiling and the upgrade path.

Do not spawn subagents, workflows, or deep research unless asked for them.

Tests are named as sentences describing behaviour
(`test_a_day_with_nothing_measurable_reports_no_pace_rather_than_zero`), and
comments explain *why*, not *what*. Match that. When you add a claim to a test
file's docstring, the test that proves it goes in the same commit.

Before you report something as done, run the suite. `python -m pytest` is 703
tests and takes under a minute.
