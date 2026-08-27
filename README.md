# TaleTrace

A reading companion for a physical book. An ESP32-CAM watches the page, the
reader points a finger at a word, and TaleTrace reads the page aloud, explains
the word they pointed at, and afterwards tells them which paragraphs took more
attention than expected.

Nothing about it is a screen-reading app. The input is a photograph of paper.

```
ESP32-CAM ──frames──┐
                    ├─► DeviceLoop ─► OCR ─► Merge Engine ─► Merge Memory
ESP32 buttons ──────┘                  └─► Gesture ─► Reading Engine
                                                       ├─► Audio Engine
                                                       ├─► AI Engine
                                                       ├─► Reading Speed
                                                       └─► Focus Analytics
```

Joining the project — human or AI? Read [AGENTS.md](AGENTS.md) first. It is the
handoff: the rules that cannot be broken, the invariants that look like bugs but
are not, what to build next, and the traps that have already cost somebody an
afternoon.

---

## Quick Start (One Command)

If you just want to run the whole project immediately without step-by-step setup, use the provided start scripts. These will automatically create a virtual environment, install dependencies, copy `.env.example`, and start both the backend and frontend servers.

**Windows:**
```bat
start.bat
```

**macOS / Linux:**
```bash
chmod +x start.sh
./start.sh
```

Once running, open the Local URL (usually `http://localhost:5173`) and log in with the seeded demo account to see real charts:
- **Email**: `demo@taletrace.app`
- **Password**: `demo1234`

---

## Two commands (Manual Setup)

If you prefer to set up manually, there are only two commands anybody needs on day one. Neither needs hardware, an API key, a dataset or a network.

**See the reading engine work:**

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python -m backend.app.simulated_session
```

It generates four synthetic pages, runs a full reading session against them
using recorded OCR responses, and prints what happened:

```
TaleTrace — simulated session
  images      4  (page_01.png … page_04.png)
  session     1 min of reading time
  clock       0.0x real per session second
  OCR         recorded Vision responses (offline)

  session time    123.0s over 1203 ticks
  frames read     110
  gestures        3
  events          5
  pages           4
  words           192
```

**See the website:**

```bash
python scripts/dev.py
```

One command, both servers — FastAPI on `127.0.0.1:8000` and the React dev server
on `localhost:5173` — with both logs in this terminal and both stopped by one
Ctrl-C. Open the URL it prints and log in as `demo@taletrace.app` / `demo1234`.
The charts you see are real reading sessions out of `taletrace.db`, which is
committed to this repo. Details in §2.4; it needs Node.js, which the reading
engine does not.

Everything below is detail on those two, on the real hardware, and on the
harnesses.

---

## 1. Setup, from nothing

### 1.1 Requirements

| Thing | Version | Why |
|---|---|---|
| Python | **3.12** (3.12.10 is what this is developed on) | 3.13 has no `mediapipe` wheel for the pinned version |
| pip | any recent | |
| Git | any | |
| Node.js | **20+** | only for the website (`scripts/dev.py`, `npm run dev`). The reading engine needs none of it. |
| OCR.Space API key | — | primary engine for live OCR; offline commands run without it |
| Google Cloud Vision API key | — | fallback engine for live OCR |
| Groq API keys (×2) | — | only for text reconstruction and Meaning Mode |
| Freesound API key | — | optional, for dynamic ambient audio retrieval |
| ESP32-CAM + ESP32 DevKit | — | only for a live session |

The dependency versions in `requirements.txt` are **capped on purpose** and the
caps are load-bearing. `mediapipe<0.10.30` because the legacy `mp.solutions`
API the fingertip detector uses was removed during the 0.10.x line, not at 1.0;
`numpy<2` because mediapipe 0.10.21 requires it; `opencv-python<4.12` because
4.12+ requires numpy>=2. Those three are one decision — lifting any one lifts
all three, and doing so silently disables tier 1 of the gesture detector. The
comments in `requirements.txt` say the same thing at greater length; read them
before bumping anything.

### 1.2 Clone and create the environment

```bash
git clone <repo-url> TaleTrace
cd TaleTrace

python -m venv .venv
```

Activate it — this differs per shell, and every command in this README assumes
it is active:

```bash
.venv/Scripts/activate          # Windows, Git Bash
.venv\Scripts\activate.bat      # Windows, cmd.exe
.venv\Scripts\Activate.ps1      # Windows, PowerShell
source .venv/bin/activate       # macOS / Linux
```

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

That install pulls a TensorFlow Lite runtime through mediapipe, so it is not
small and not fast. Expect a few minutes on a first run.

### 1.3 Configuration

```bash
cp .env.example .env            # copy .env.example .env  on cmd.exe
```

`.env` is git-ignored and must stay that way — this is a group project and the
file holds live credentials. `.env.example` is the tracked template and holds
none.

Everything in `.env` is optional except where noted:

| Variable | Needed for | Absent behaviour |
|---|---|---|
| `OCR_SPACE_API_KEY` | **Live OCR (Primary).** | Falls back to Google Vision if absent. |
| `GOOGLE_VISION_API_KEY` | **Live OCR (Fallback).** | A live session has no text at all if both OCR keys are absent. Offline commands use recorded responses. |
| `GROQ_API_KEY_1` | AI Engine only — Meaning Mode, explanations, session review | Meaning Mode cannot explain a word. Reading still works. |
| `GROQ_API_KEY_2` | Merge Engine only — text reconstruction, OCR cleanup, same-page detection | Page text falls back to raw OCR; page turns use geometry alone. Reading still works. |
| `GROQ_API_KEY` | Legacy single key | Read only where the two above are unset, so an old `.env` keeps working. |
| `FREESOUND_API_KEY` | Dynamic ambient audio fallback | Falls back to the local curated starter pack or silence. |
| `ESP32_CAM_CAPTURE_URL` | Live camera, e.g. `http://192.168.1.200/capture` | Reported unconfigured; the runtime still starts. |
| `ESP32_BUTTONS_URL` | Live buttons, e.g. `http://192.168.1.26:8080/buttons` | Reported unconfigured; a live session substitutes a scheduled reader (see §4). |
| `AUDIO_PROVIDER` | `edge` (default, neural voices, needs network), `offline` (pyttsx3, OS voices), `fake` (tests) | |
| `EDGE_TTS_VOICE` | Which Edge voice, default `en-US-AriaNeural` | |

**The two Groq keys are not interchangeable and must not be swapped.** The
Merge Engine calls Groq several times a second while the camera runs; the AI
Engine calls it once per reader request. On a shared key the camera loop
exhausts the rate limit and the *reader* is the one who sees the error. Each
subsystem builds its own client from its own key so neither can take the other
down.

### 1.4 Check the install

```bash
python -m pytest -q
```

Expected: **703 passed**. No API key, no network and no hardware are required —
if this does not pass on a clean checkout, stop here, because nothing further
will make sense.

The website has its own small suite, which needs Node.js:

```bash
cd frontend && npm install && npm test
```

Expected: **7 passed**. It covers the one thing that is easy to get silently
wrong — that the Analysis page's numbers come from the server and there is no
local fallback left to quietly replace them. See §2.4.

---

## 2. Running it

### 2.1 Simulated session — no hardware, no key

The zero-argument form generates its own pages and forces offline OCR:

```bash
python -m backend.app.simulated_session
```

With your own images:

```bash
# one page
python -m backend.app.simulated_session page.jpg

# a folder, one page at a time, fifteen minutes of reading time
python -m backend.app.simulated_session pages/ --minutes 15

# watch it happen at human speed instead of instantly
python -m backend.app.simulated_session page.jpg --realtime

# recorded OCR responses instead of live Vision: no key, no network
python -m backend.app.simulated_session pages/ --offline

# at most three images from the folder
python -m backend.app.simulated_session pages/ --limit 3

# module logs
python -m backend.app.simulated_session -v
```

| Flag | Default | Meaning |
|---|---|---|
| `target` | generate synthetic pages | an image, or a directory of images |
| `--minutes` | `1` | session length in **reading time**, not waiting time |
| `--speed` | `0` | real seconds waited per session second; `0` waits not at all |
| `--realtime` | off | same as `--speed 1` |
| `--offline` | off (forced **on** with no target) | replay recorded Vision responses; no Groq |
| `--limit` | all | use at most this many images |
| `-v` | off | show module logs |

A simulated session runs the *same* `DeviceLoop`, runtime and modules as a live
one. The only differences are three constructor arguments:

| | camera | buttons | clock |
|---|---|---|---|
| `live_session` | `Esp32Camera` | `Esp32Buttons` | `RealClock` |
| `simulated_session` | `VirtualCamera` | `ScriptedButtons` | `VirtualClock` |

Nothing downstream is told which it got. That is what makes a simulated session
evidence about a real one rather than a separate thing that resembles it.

### 2.2 Test the hardware first

Before any real session, ask the rig what it can do. There is a diagnostic script to check whether everything is responding correctly before you run the actual application:

```bash
python scripts/hardware_check.py
```

This checks Python, required modules, environment variables, git integrity, network subnet compatibility, and probes the hardware URLs (Camera and Buttons). Fix every `[FAIL]` or `[WARN]` you intend to use before going on.

Alternatively, you can run the live session preflight check:

```bash
python -m backend.app.live_session --check
```

Output, on a machine with keys but no rig plugged in:

```
TaleTrace — live session
  OK    Google Vision         ready
  OK    Groq — Merge Engine   ready
  OK    Groq — AI Engine      ready
  FAIL  ESP32-CAM             http://192.168.1.200/capture
  FAIL  ESP32 buttons         http://192.168.1.26:8080/buttons — no answer; --buttons virtual rehearses instead
```

`--check` exists because "the session produced no text" has four causes that
look identical from outside — no camera, no key, no buttons, no book in frame —
and finding out which by starting a session and waiting is the slow way. It
exits `0` when every row is OK and `1` otherwise, so it works in CI.

Each row is probed for **reached**, not merely configured. A URL pointing at a
device that is powered off is the most common failure and it looks exactly like
a correct configuration until a frame is asked for. Keys are reported present or
absent and never printed.

Fix every `FAIL` you intend to use before going on. §3 covers the wiring and §7
lists what each failure means.

### 2.3 Live session — with the rig

```bash
# a full session, until Ctrl-C
python -m backend.app.live_session

# stop after two minutes
python -m backend.app.live_session --seconds 120

# real camera, scheduled reader instead of real buttons
python -m backend.app.live_session --buttons virtual

# never substitute: trust the wiring even if the probe fails
python -m backend.app.live_session --buttons hardware
```

Every finished session — live or simulated — writes one row to `taletrace.db`,
which is what the website reads. Nothing else writes reading history.

### 2.4 The website

```bash
python scripts/dev.py
```

That is the whole command. It starts the backend and the React dev server, keeps
both logs in one terminal, and stops both on Ctrl-C — including when one of them
dies on its own, which is the case two terminals handle worst. On the first run
it installs the frontend's dependencies for you. If port 8000 is already busy it
says so and stops, instead of leaving you with a website whose every request
fails against yesterday's backend.

Open the `Local:` URL Vite prints (normally `http://localhost:5173`) and log in:

```
demo@taletrace.app / demo1234
```

Two terminals, if you prefer them:

```bash
python -m uvicorn backend.app.main:app --reload    # terminal 1
cd frontend && npm run dev                          # terminal 2
```

**What the website is.** A companion, not a reader. There is no "start reading"
button anywhere, because reading happens on the rig — the website shows what the
rig produced: the device's live status, today's pages and lookups, every past
session with its true reading time and difficulty, the AI summary, and the
Analysis charts.

**Where its numbers come from.** `sessions` rows in `taletrace.db`, written by
`live_session` and `simulated_session`. The Analysis page aggregates them per
calendar day — reading time, pages, meaning lookups, the pace trend and the
difficulty trend — with the same four filters the page has always had (Today,
Last 7 Days, Last 30 Days, All Time). Nothing is recomputed: the pace is the
persisted `session_wpm`, the words are the persisted `words_read`, the difficulty
is the persisted verdict. If the backend is down the page shows its error state;
it never falls back to invented figures.

**The seeded history.** `taletrace.db` is committed, and the twelve sessions in
it are a deterministic corpus so everyone's charts look the same and all four
time filters have something to select:

```bash
python scripts/seed_history.py --list       # what is in the database right now
python scripts/seed_history.py              # refresh the seeded rows, keep real ones
python scripts/seed_history.py --reset      # delete EVERY session, then seed
```

Every seeded row carries `source_reference = "seed:reading-history"`, which is
the only way they are ever found — there is no "ignore small sessions" rule
anywhere, because a page read at breakfast is small too. Their numbers are not
typed in: each entry says how the reading went and the row is then produced by
the same `summarize_session` a real session uses, so the corpus also fails
loudly if the measurement chain ever changes. Both commands back the database up
to `.taletrace_cache/` first. `--reset` deletes your own sessions too.

Three quirks worth knowing:

- Accounts are real, server-side, and protected by HTTP-only session cookies. Argon2 is used for password hashing.
- Quizzes and Flashcards still use sample banks. The real data is already being
  persisted per session (`review_payload`); the endpoint that merges several
  sessions is not built yet.
- `frontend/src/services/api.js` is the only file that talks to the backend.
  Pages and components read it and nothing else.

### 2.5 The API server

```bash
uvicorn backend.app.main:app --reload
```

Then `http://127.0.0.1:8000/docs` for the interactive schema. The HTTP surface
is for inspection and for the frontend; the reading loop itself does not go
through it.

<details>
<summary>Every route</summary>

```
GET    /health

# the website's own API — everything under /api. Plain JSON, no envelope.
POST   /api/auth/signup          POST   /api/auth/login
POST   /api/auth/logout          GET    /api/me
PATCH  /api/me
GET    /api/device/status
GET    /api/dashboard
GET    /api/analysis?range=today|week|month|all
GET    /api/reading-test         POST   /api/reading-test
POST   /api/reading-speed/preset
GET    /api/sessions
GET    /api/sessions/{id}        PATCH  /api/sessions/{id}
DELETE /api/sessions/{id}
POST   /api/folders              PATCH  /api/folders/{id}
DELETE /api/folders/{id}

POST   /upload_frame
POST   /ocr/process
POST   /gesture/select

POST   /session/start
POST   /session/end
GET    /session/current

POST   /ai/explain
POST   /ai/image-decision
POST   /ai/novel-mode
POST   /ai/session-summary

POST   /audio/start              POST   /audio/stop
POST   /audio/pause              POST   /audio/resume
POST   /audio/seek               POST   /audio/refresh-queue
GET    /audio/status             GET    /audio/sessions
DELETE /audio/session
GET    /audio/voices             POST   /audio/voice
GET    /audio/profiles           POST   /audio/profile

POST   /reading-speed/calibrate
POST   /reading-speed/baseline
GET    /reading-speed/baseline/{reader_id}
GET    /reading-speed/sessions
POST   /reading-speed/sessions/{session_id}/start
POST   /reading-speed/sessions/{session_id}/content
POST   /reading-speed/sessions/{session_id}/pointer
POST   /reading-speed/sessions/{session_id}/lookup
POST   /reading-speed/sessions/{session_id}/meaning-mode
POST   /reading-speed/sessions/{session_id}/pause
POST   /reading-speed/sessions/{session_id}/resume
GET    /reading-speed/sessions/{session_id}/progress
GET    /reading-speed/sessions/{session_id}/prediction
POST   /reading-speed/sessions/{session_id}/finish
DELETE /reading-speed/sessions/{session_id}
```

The Focus Analytics Engine has no routes on purpose — it observes events and is
read at the end of a session, never polled.

</details>

---

## 3. The hardware

Two devices, each with its own firmware sketch under
`backend/app/OCRandGESTURE/`:

| Device | Sketch | Serves | Endpoint |
|---|---|---|---|
| ESP32-CAM | `espcam/Almost_final.ino` | port 80 | `GET /capture` → one JPEG |
| ESP32 DevKit (buttons) | `esp32/esp32.ino` | port 8080 | `GET /buttons` → button state JSON |

### 3.1 Flashing and wiring

1. Flash both sketches with the Arduino IDE, setting your Wi-Fi SSID and
   password at the top of each.
2. Put both devices on the **same network as the machine running TaleTrace**.
   A phone hotspot is fine; a guest network with client isolation is not.
3. Read each device's IP from the serial monitor at boot.
4. Put those into `.env`:
   ```
   ESP32_CAM_CAPTURE_URL="http://<cam-ip>/capture"
   ESP32_BUTTONS_URL="http://<devkit-ip>:8080/buttons"
   ```
5. Confirm by hand before involving TaleTrace:
   ```bash
   curl -o frame.jpg http://<cam-ip>/capture
   curl http://<devkit-ip>:8080/buttons
   ```
6. Then:
   ```bash
   python -m backend.app.live_session --check
   ```

### 3.2 The two controls

| Control | Type | Does |
|---|---|---|
| Momentary button | press | resolve the word under the fingertip and re-read from there |
| Toggle switch | hold | Meaning Mode: pause narration, explain the word being pointed at |

The rig has **only these two**. `SESSION_PAUSED` / `SESSION_RESUMED` and audio
on/off exist in the event vocabulary and the Reading Engine handles them, but
nothing on the hardware produces them — those paths are driven by
`VirtualButtons` today. That is a wiring gap, not a software one.

### 3.3 Framing the page

- The whole page in frame, filling it as much as possible.
- Even light, no hard shadow across the text, no glare from a lamp.
- The finger **on** the word, not hovering above it — the fingertip's position
  is the query.
- Only the fingertip needs to be visible. A partly-visible hand is fine.

---

## 4. Hardware auto-detection

Each device is detected on its own and substituted on its own, because a rig
arrives in halves and is repaired in halves.

| Camera | Buttons | What runs |
|---|---|---|
| reachable | reachable | Full hardware. Nothing announced. |
| reachable | not reachable | **Real camera + scheduled reader.** Announced. |
| not reachable | either | Refuses to start; use `simulated_session`. |

The half-built case is the dangerous one. With unreachable buttons and no
detection, the session runs, reads frames, and can never fire a reading update
or Meaning Mode — a blind session that is indistinguishable from a working one
until someone notices the gesture count is zero. So the buttons fall back to a
repeating schedule (point at a word, hold Meaning Mode, release, every 30s for
the length of the run) and the substitution is **printed, never silent**:

```
  buttons: not reachable — running a scheduled rehearsal instead, so the camera
  can still be tested end to end
```

There is deliberately **no virtual-camera fallback** in `live_session`. A live
session that quietly replays JPEGs is a simulation wearing the wrong label, and
that is how a hardware bug survives a green test. `simulated_session` already
does that job honestly.

Detection is a guess about the physical world, so it can be overridden:

```bash
python -m backend.app.live_session --buttons auto       # default: probe, fall back
python -m backend.app.live_session --buttons hardware   # never substitute, never probe
python -m backend.app.live_session --buttons virtual    # never probe, always rehearse
```

`--buttons hardware` is for the case where the probe is wrong — a device that is
slow to answer on first contact, say. It trusts the wiring, so a genuinely dead
endpoint then produces a session with no events, which is the correct outcome of
being told not to second-guess.

### 4.1 Devices dropping mid-session

Already handled, and worth knowing so a flaky link is not mistaken for an OCR
problem:

- `Esp32Camera.frame()` returns `None` on any failure rather than raising, with
  a 1.5s timeout so a device that has gone away cannot stall the control loop.
  The loop skips that pass and tries again on the next tick.
- `Esp32Buttons` reads an unreachable device as "nothing pressed, not
  reachable", and emits `CAMERA_OFF` / `CAMERA_ON` edges when reachability
  changes — so the rig announces its own disappearance.
- The end-of-session summary reports lost frames, because a session that read 4
  frames out of 600 attempts is a Wi-Fi problem that otherwise presents as a low
  page count:
  ```
    devices         esp32_cam + esp32_buttons
    frames read     412
    frames lost     188  (camera unreachable or returned no image)
  ```

---

## 5. Verification and harnesses

### 5.1 Everything, in order

```bash
python -m scripts.verify_all                          # unit suite only
python -m scripts.verify_all photo.jpg                # + acceptance + differential
python -m scripts.verify_all photo.jpg --offline      # no network at all
python -m scripts.verify_all photo.jpg --hardware     # + probe the rig
python -m scripts.verify_all photo.jpg --keep-going   # do not stop at first failure
```

| Flag | Meaning |
|---|---|
| `image` | a photograph of a page with a finger pointing at a word |
| `--offline` | cached Vision response, no Groq call on either key |
| `--hardware` | also run `live_session --check` |
| `--keep-going` | run every stage even after one fails |
| `--fresh-ocr` | call Vision again even though a cached response exists |
| `--json` | where the acceptance run writes its full result |

### 5.2 One photograph, every stage

The primary acceptance tool. Runs the whole production chain on one image and
shows each stage's output, including Reading Focus Analysis.

```bash
python -m scripts.validate_pipeline photo.jpg
python -m scripts.validate_pipeline photo.jpg --no-groq      # no reconstruction, no explanation
python -m scripts.validate_pipeline photo.jpg --no-ai        # skip the AI call, run the code path
python -m scripts.validate_pipeline photo.jpg --fresh-ocr    # bypass the OCR cache
python -m scripts.validate_pipeline photo.jpg --json out.json
```

Other flags: `--book-title`, `--audience`, `--ai-timeout`, `--sentence-seconds`
(how long the fake speech provider takes per sentence, so narration is still in
flight when Meaning Mode interrupts it).

**A failure here is a product defect, not a harness problem.** Assertions do not
get weakened to make it pass.

### 5.3 A folder of photographs

```bash
python -m scripts.validate_corpus photos/
python -m scripts.validate_corpus photos/ --limit 20
python -m scripts.validate_corpus photos/ --cached-only      # never call Vision
python -m scripts.validate_corpus photos/ --min-finger-rate 0.8
python -m scripts.validate_corpus photos/ --json corpus.json
```

### 5.4 Migrated vs reference

`backend/app/OCRandGESTURE/` is the original implementation, kept as the
functional specification. This runs both on the same photograph and compares:

```bash
python -m scripts.equivalence_probe photo.jpg
python -m scripts.equivalence_probe photo.jpg --no-groq      # deterministic stages only
python -m scripts.equivalence_probe photo.jpg --baseline     # reference vs itself
```

### 5.5 Long sessions and slow failures

Drives the real `DeviceLoop` tick by tick for an hour of session time, sampling
as it goes, looking for leaks, duplicated Merge Memory, unbounded queues and
stalled playback.

```bash
python -m scripts.stress_session pages/                              # 60 session-minutes
python -m scripts.stress_session pages/ --minutes 120 --samples 60
python -m scripts.stress_session pages/ --no-audio
python -m scripts.stress_session pages/ --json stress.json
```

The invariants it checks, each of which has a test proving it can fail:

- Merge Memory stops growing on a still page
- the memory version never goes backwards
- the pointer never turns back to an earlier page
- pending events drain
- the audio queue is bounded — and a silent session is reported as **untested**,
  not as bounded
- playback never sits `FINISHED` with sentences still queued
- the heap stops growing
- the session actually read something

Tuning knobs: `--word-drift`, `--max-pending`, `--max-queue`,
`--heap-kb-per-tick`, `--seconds-per-page`, `--speed`, `--limit`, `--verbose`.

### 5.6 Synthetic pages

Generates page images **and** the Vision responses that describe them, so every
offline harness runs on a clean checkout with no key:

```bash
python -m scripts.synthetic_page                       # 4 pages, default location
python -m scripts.synthetic_page --pages 40
python -m scripts.synthetic_page --out pages/ --force
```

They have no camera noise, no page curl, no shadow, no perspective and no motion
blur. They are good for the pointer, the queue and Merge Memory, and **worthless
for judging whether OCR can read a real page**.

### 5.7 The OCR cache

Google Vision is called **once per distinct image**, keyed on the preprocessed
bytes, and the response is cached under `.taletrace_cache/vision/`. That is a
standing cost requirement, not an optimisation.

There is no command for it — `scripts/vision_cache.py` is the shared fetching
layer every harness imports, so `validate_pipeline` and `equivalence_probe`
cannot end up comparing against two different Vision responses for the same
photograph. To force a fresh call, pass `--fresh-ocr` to the harness. To discard
everything, delete `.taletrace_cache/vision/`.

---

## 6. Layout

```
backend/app/
  live_session.py          production entry point — the rig
  simulated_session.py     the same runtime, virtual devices
  main.py                  FastAPI app
  api/companion.py         the website's API — /api/*, plain JSON, no arithmetic
  core/                    settings, environment loading, logging
  shared/                  clock, events, constants, Groq key routing
  modules/
    image_receiver/        Esp32Camera, Esp32Buttons, VirtualCamera,
                           VirtualButtons, ScriptedButtons, and the protocols
                           they satisfy
    preprocessing/         frame enhancement before OCR
    ocr/                   Google Vision provider, parser, cache, replay adapter
    merge_memory/          reconstruction, same-page detection, pointer sync
    gesture_engine/        fingertip → word
    reading_engine/        DeviceLoop, runtime, the session state machine
    audio_engine/          playback state machine, speech providers, queue
    ai_engine/             Meaning Mode, explanations, session review
    reading_speed/         calibration, baselines, predictions
    focus_analytics/       Reading Focus Analysis Engine
    session/               session lifecycle
    database/              models, recording.py (one finished session → one row),
                           analysis.py (rows → the five Analysis charts)
  OCRandGESTURE/           the reference implementation + ESP32 sketches
frontend/                  the website (React + Vite)
  src/services/api.js      the only file that talks to the backend
  src/services/mockBackend.js  what is still faked: accounts, quizzes, flashcards
scripts/                   the harnesses in §5, plus dev.py and seed_history.py
tests/                     703 tests
docs/                      architecture, module contracts, verification
taletrace.db               the reading history — committed on purpose (§2.4)
.taletrace_cache/          OCR responses, generated pages, database backups
                           (derived; deletable)
```

### 6.1 Two things worth knowing before changing code

[AGENTS.md](AGENTS.md) has the full list — sentinel values, the two Groq keys,
the timezone handling, the ordered roadmap. These two are the ones most often
got wrong.

**Reading Focus Analysis is not distraction detection.** It never answers "was
the reader distracted?" — it answers "which sections required more attention
than expected?". It observes events, never polls, and never reconstructs text.
Paragraphs are the unit. Idle time is reported as "Possible Idle Time —
reading paused longer than expected", never as "you were distracted". The
wording is part of the contract.

**`OCRandGESTURE/` is the specification.** The port is behaviour-preserving: the
same ESP32 image, Vision result, reading position and button events must produce
the same Merge Memory, Reading Pointer, page detection, AI requests and audio
queue. Working logic is migrated as written, not improved, unless there is an
actual bug. `pytest.ini` limits collection to `tests/` because
`OCRandGESTURE/`'s own tests reach for the real ESP32 over HTTP — those files
are read, not run.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pip install` fails on mediapipe | Python 3.13+ | Use Python 3.12. |
| Gesture detection only ever uses the HSV fallback | mediapipe was upgraded past 0.10.21 | `pip install "mediapipe<0.10.30" "numpy<2"`. See §1.1. |
| `Cannot start: OCR has no API key` | `OCR_SPACE_API_KEY` and `GOOGLE_VISION_API_KEY` empty | Set one in `.env`, or run an offline command. |
| `Cannot start: no camera` | camera unreachable | `curl http://<cam-ip>/capture`. Check power, Wi-Fi, and that the URL includes `/capture`. |
| Live session runs but gesture count is 0 | buttons unreachable and `--buttons hardware` given | Drop the flag, or fix the endpoint. `--check` says which. |
| `frames lost` is most of `frames read` | Wi-Fi, not OCR | Move the camera closer to the router; lower the frame size in the sketch. |
| Meaning Mode explains nothing | `GROQ_API_KEY_1` empty | Set it. Reading continues without it. |
| Page text looks like raw OCR | `GROQ_API_KEY_2` empty | Set it. Reading continues without it. |
| `UnicodeEncodeError` in a terminal | cp1252 console | Both entry points force UTF-8; if you hit this elsewhere, `set PYTHONUTF8=1`. |
| An offline command says an image has no cached response | never OCR'd | Run it once online, or use `scripts.synthetic_page`. |
| Tests hang | ran bare `pytest` from a directory that ignores `pytest.ini` | Run `python -m pytest -q` from the repo root. |
| `scripts/dev.py` says something is already listening on 8000 | a backend from an earlier run | `netstat -ano \| findstr :8000` then `taskkill /F /PID <pid>`; `lsof -ti :8000 \| xargs kill` on Unix. |
| `scripts/dev.py` says npm was not found | no Node.js | Install Node 20+, or run the backend alone with `python -m uvicorn backend.app.main:app --reload`. |
| Vite starts on 5174 or higher | 5173 is taken by another dev server | Nothing to fix — open the URL Vite prints. Its `/api` proxy follows it. |
| Every page on the website shows its error state | the backend is not running | Start it. There is no offline fallback by design — invented figures on screen are worse than an error. |
| The website loads but Analysis is empty | no sessions in `taletrace.db` for that range | Try All Time; then `python scripts/seed_history.py`. §2.4. |
| Analysis has bars but a gap in the pace or difficulty line | that day had nothing measurable | Working as intended: a session too short to time reports no pace, and an unrated page reports no difficulty. A gap is true; a zero would not be. |
| Login fails with the demo password | `localStorage` was cleared, or a different browser profile | Sign up again — accounts are per-browser (§2.4). |
| `taletrace.db` conflicts on `git pull` | it is a binary and two people changed it | Take either side, then `python scripts/seed_history.py --reset`. Never resolve it by hand. |

---

## 8. Command reference

Every command in one place. All assume an activated venv at the repo root.

```bash
# ── setup ────────────────────────────────────────────────────────────────────
python -m venv .venv
.venv/Scripts/activate                    # source .venv/bin/activate on Unix
pip install -r requirements.txt
cp .env.example .env

# ── the two commands ─────────────────────────────────────────────────────────
python -m backend.app.simulated_session               # the reading engine
python scripts/dev.py                                 # the website: both servers

# ── tests ────────────────────────────────────────────────────────────────────
python -m pytest -q                                   # all 703
python -m pytest -q tests/test_device_integration.py   # one file
python -m pytest -q -k DeviceDetection                 # one class
python -m pytest -q -x -vv                             # stop at first failure, verbose
cd frontend && npm test                                # the website's 7

# ── sessions ─────────────────────────────────────────────────────────────────
python -m backend.app.simulated_session                          # synthetic, offline
python -m backend.app.simulated_session pages/ --minutes 15
python -m backend.app.simulated_session page.jpg --realtime
python -m backend.app.simulated_session pages/ --offline --limit 3

python -m backend.app.live_session --check                       # probe and exit — run this FIRST
python -m backend.app.live_session                               # until Ctrl-C
python -m backend.app.live_session --seconds 120
python -m backend.app.live_session --buttons virtual             # real cam, scheduled reader
python -m backend.app.live_session --buttons hardware            # never substitute

# ── API ──────────────────────────────────────────────────────────────────────
uvicorn backend.app.main:app --reload                            # /docs for the schema
curl "http://127.0.0.1:8000/api/analysis?range=week"             # what Analysis draws

# ── the website ──────────────────────────────────────────────────────────────
python scripts/dev.py                                            # both servers, one Ctrl-C
cd frontend && npm run dev                                       # frontend alone
cd frontend && npm test                                          # its tests
cd frontend && npm run lint
cd frontend && npm run build                                     # production bundle

# ── the database ─────────────────────────────────────────────────────────────
python scripts/seed_history.py --list                            # what is in there
python scripts/seed_history.py                                   # refresh seeded rows
python scripts/seed_history.py --reset                           # wipe all, then seed

# ── verification ─────────────────────────────────────────────────────────────
python -m scripts.verify_all
python -m scripts.verify_all photo.jpg --offline
python -m scripts.verify_all photo.jpg --hardware --keep-going

python -m scripts.validate_pipeline photo.jpg
python -m scripts.validate_pipeline photo.jpg --no-groq --json out.json

python -m scripts.validate_corpus photos/ --cached-only
python -m scripts.equivalence_probe photo.jpg --no-groq

python -m scripts.stress_session pages/ --minutes 60 --samples 40
python -m scripts.stress_session pages/ --no-audio --json stress.json

python -m scripts.synthetic_page --pages 40

# ── hardware, by hand ────────────────────────────────────────────────────────
curl -o frame.jpg http://<cam-ip>/capture
curl http://<devkit-ip>:8080/buttons
```
