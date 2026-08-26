# TaleTrace System Architecture

TaleTrace consists of two independent but connected boundaries: the **Device Runtime** (where reading happens) and the **Web Runtime** (the companion website and its API).

## 1. Boundary Map

```text
               ┌─────────────────────┐
               │    WEB RUNTIME      │
               │ (React + FastAPI)   │
               └─────────┬───────────┘
                         │
      HTTP/REST (Real Auth & Cookie Sessions)
                         │
               ┌─────────▼───────────┐
               │   DATABASE (SQLite) │
               │   (Shared Storage)  │
               └─────────▲───────────┘
                         │
      Database writes (1 row per finished session)
                         │
               ┌─────────┴───────────┐
               │   DEVICE RUNTIME    │
               │   (Reading Engine)  │
               └─────────┬───────────┘
                         │
            Hardware/Virtual Sensors
      (ESP32-CAM, ESP32 Buttons, Clocks)
```

## 2. Device Runtime

The Device Runtime runs the core reading pipeline against physical (or simulated) hardware. It operates continuously in a control loop (`DeviceLoop`) and is **unauthenticated** because it runs locally on a trusted rig, producing data for whichever reader is currently active.

The pipeline processes physical inputs into semantic reading history:

```text
ESP32-CAM frame
  → image_receiver/     (accepts the frame)
  → preprocessing/      (deskew, enhance)
  → ocr/                (Google Vision → words with bounding boxes)
  → gesture_engine/     (fingertip → the word being pointed at)
  → merge_memory/       (stitches pages, detects same-page, syncs pointer)
  → reading_engine/     (the session: pointer, lookups, state machine)
  → audio_engine/       (TTS narration)
  → ai_engine/          (Meaning Mode + end-of-session review)
  → reading_speed/      (baseline, session WPM, per-page difficulty)
  → focus_analytics/    (Reading Focus Analysis)
  → database/           (persists exactly one row per finished session)
```

## 3. Web Runtime

The Web Runtime (`backend/app/main.py` + `backend/app/api/companion.py` and `frontend/`) is a review companion. There is no "start reading" button here. It exposes data aggregated from `taletrace.db`.

### Authentication and Ownership
The API server is protected by **real server-side authentication**:
- Accounts are created via `/api/auth/signup` and logged in via `/api/auth/login`.
- Sessions use secure, HTTP-only cookies (`taletrace_session`).
- Password hashing is handled by Argon2.
- Data isolation: A reader can only access their own history, sessions, profiles, and analytical views.
- The `get_current_reader` dependency ensures all protected endpoints automatically reject unauthenticated access (returning HTTP 401).

## 4. Shared Vocabulary

Both runtimes exchange data via a shared SQLite database (`taletrace.db`). The database schema is defined by SQLAlchemy models (`backend/app/modules/database/models.py`).

- `Reader`: Owns sessions, folders, and profile configuration.
- `Session`: A finished reading session (includes `session_wpm`, `words_read`, `review_payload`, `difficulty`, etc.).
- `Folder`: Organization grouping for sessions.
- `Token`: Manages session tokens for HTTP-only cookie rotation.
