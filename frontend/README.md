# TaleTrace — Frontend

Companion web app for the TaleTrace physical smart bookmark. Built with React + Vite,
react-router-dom, and recharts, styled with plain CSS custom properties (no Tailwind).

It is a **companion, not a reader**: there is no "start reading" button anywhere,
because reading happens on the ESP32 rig. This app shows what the rig produced.

New here? [AGENTS.md](../AGENTS.md) at the repo root is the handoff for the whole
project — the rules, the invariants, and what to build next.

## Running it

From the repo root, one command for this and the backend together:

```bash
python scripts/dev.py
```

Or this alone, with a backend already running on `127.0.0.1:8000`:

```bash
npm install
npm run dev
npm test          # the api.js contract tests
npm run lint
```

Then open the printed local URL (usually http://localhost:5173). `vite.config.js`
proxies `/api` to the backend, so the browser only ever talks to one origin and no
`VITE_API_URL` is needed locally.

To build for production: `npm run build` (outputs to `dist/`).

## Demo login

```
demo@taletrace.app / demo1234
```

The account is seeded locally; the reading history it shows is **not** — that comes
from the backend's `sessions` table. Signing up with a brand-new email gives you the
same reading history and a fresh onboarding flow, because the server holds one reader
and the account is only a front door.

If the charts and sessions are empty, the database has no sessions in it: run
`python scripts/seed_history.py` at the repo root, or read a page on the rig.

## What is real and what is still mocked

`src/services/api.js` is the **only** file that talks to the backend — every page and
component imports from it and nothing else. It has two sources on purpose:

| Real, via `fetch()` | Still `mockBackend.js` |
|---|---|
| the reader profile and settings | accounts: signup, login, logout |
| device status (a live probe of the rig) | quizzes: generation and scoring |
| the dashboard | flashcards |
| **the five Analysis charts** | |
| sessions, folders, session details | |
| the reading-speed test and baseline | |

Accounts stay in `localStorage` because there is no authentication server-side. No
password ever leaves the browser, and nothing here should be exposed to a network.

Quizzes and flashcards use sample banks. Their real content is already persisted per
session by the AI Engine (`review_payload`); what is missing is the endpoint that
merges several sessions' worth, so no new AI call is ever needed.

**The Analysis charts have no fallback.** `mockBackend.js` used to aggregate seven
fabricated sessions — reading times, pages, lookups, WPM and difficulty all from
`Math.random()` — and that code is deleted, not merely unreferenced. If the backend
is down, the page shows its error state. Invented figures that render identically to
measured ones are worse than an error, because nobody can tell by looking which they
are seeing. `src/services/api.analysis.test.js` is what keeps that true.

Shape adapting happens in `api.js` and only there: the backend speaks in epoch
milliseconds and `low`/`medium`/`high`, the pages want formatted dates and
`Easy`/`Medium`/`Hard`. Dates are formatted in the browser, because the browser is the
only part of this system that knows the reader's timezone.

## Project structure

```
src/
├── components/
│   ├── layout/       AppShell, Sidebar, Header, MobileNavigation
│   ├── ui/            Button, Card, Input, Toggle, Checkbox, Modal, Dropdown,
│   │                   Badge, LoadingState, EmptyState, ErrorState, Skeleton
│   ├── analytics/     AnalyticsChart, TimeFilter
│   ├── sessions/      SessionSelector, SessionItem, FolderItem, CreateFolderModal
│   ├── quiz/          QuizQuestion, QuizViewer
│   ├── flashcards/    FlashcardViewer
│   └── onboarding/    ProgressIndicator, SpeedSelector, ReaderTypeSelector
├── pages/             Login, SignUp, ProfileSetup, Dashboard, Analysis, Quizzes,
│                       Flashcards, Sessions, SessionDetails, Settings
├── context/           AuthContext, ThemeContext
├── services/          api.js (the only backend caller), mockBackend.js (accounts,
│                       quizzes, flashcards), api.analysis.test.js
└── styles/            global.css (design tokens + shared components), plus one
                        stylesheet per feature area
```

## Notes

- Routing follows the spec exactly: `/login`, `/signup`, `/setup`, `/dashboard`,
  `/analysis`, `/quizzes`, `/flashcards`, `/sessions`, `/sessions/:sessionId`,
  `/settings`.
- Returning users with a completed profile skip `/setup` and land on `/dashboard`;
  route guards live in `src/components/RouteGuards.jsx`.
- Folders are single-level only, per spec — no nested folders.
- No page or component computes a reading metric. The five Analysis charts, the
  session difficulty and the WPM are all produced server-side and rendered here;
  quiz scoring is done in the mock so the answer key never reaches the browser.
- A `null` in a chart series is a gap on purpose: a day whose sessions were too
  short to time has no pace, and one whose pages went unrated has no difficulty.
  Rendering either as `0` would invent a measurement.
- Light/dark mode is a CSS custom-property swap via `data-theme` on `<html>`.
