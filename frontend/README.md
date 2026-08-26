# TaleTrace — Frontend MVP

Companion web app for the TaleTrace physical smart bookmark. Built with React + Vite,
react-router-dom, and recharts, styled with plain CSS custom properties (no Tailwind).

## Running it

```bash
npm install
npm run dev
```

Then open the printed local URL (usually http://localhost:5173).

To build for production: `npm run build` (outputs to `dist/`).

## Demo login

The app ships with a seeded demo account so you can see the full experience
immediately, with sample sessions, folders, and analytics already in place:

```
demo@taletrace.app / demo1234
```

Signing up with a brand-new email starts you on the empty states instead
(no sessions/folders yet), matching the "new user" flow in the spec.

## About the mock backend

There's no real TaleTrace server yet, so `src/services/mockBackend.js` stands in for
one. It's the **only** place in the app that does anything the brief assigns to the
backend: calculating WPM, generating quizzes/flashcards, scoring quizzes, computing
analytics, etc. Everything is persisted to `localStorage` so state survives a refresh.

`src/services/api.js` is a thin pass-through in front of it — every page and component
only ever imports from `api.js`. When a real backend exists, that's the only file that
needs to change; no page or component should need to be touched.

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
├── services/          api.js (public interface), mockBackend.js (implementation)
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
- The five Analysis charts, quiz scoring, and flashcard content are all generated
  server-side (in the mock) and simply rendered by the frontend.
- Light/dark mode is a CSS custom-property swap via `data-theme` on `<html>`.
