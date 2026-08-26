// ============================================================================
// TaleTrace — API SERVICE LAYER
// ----------------------------------------------------------------------------
// Pages and components only ever talk to this file, never to the backend or the
// mock directly. That contract is what makes this file the whole integration:
// no page or component below changed when the real backend arrived.
//
// Two sources, on purpose
// -----------------------
//   fetch()  →  everything the ESP32 rig produced or the reading engine
//               measured: sessions, folders, the reading-speed baseline,
//               whether the device is on the network.
//   mock     →  accounts, and the pages whose backend does not exist yet:
//               Analysis (needs multi-day history), Quizzes and Flashcards
//               (the data is being persisted per session, but the endpoint that
//               merges several sessions is not built).
//
// Accounts stay in localStorage because there is no authentication server-side —
// no password ever leaves the browser, and the backend holds exactly one reader.
// So `userId` is still the first argument everywhere, and this file drops it on
// the wire. When real auth arrives it becomes a session cookie and not one call
// site changes.
//
// Shape adapting happens here and only here. The backend speaks in epoch
// milliseconds and its own difficulty vocabulary; the pages want formatted dates
// and Easy/Medium/Hard. Dates are formatted *here*, in the browser, because the
// browser is the only thing in this system that knows the reader's timezone.
// ============================================================================

import * as mock from './mockBackend';

const BASE = import.meta.env.VITE_API_URL ?? '/api';

// FastAPI puts the message in `detail` — a string for our own HTTPExceptions, an
// array of field errors for a request that failed validation. Every page renders
// `error.message` through <ErrorState>, so both have to end up as one sentence.
function messageFrom(payload, status) {
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ');
  return `Request failed (${status})`;
}

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // 204 has no body at all, and calling .json() on it throws — which would turn
  // every successful delete into an error toast.
  const text = res.status === 204 ? '' : await res.text();
  const payload = text ? JSON.parse(text) : null;

  if (!res.ok) throw new Error(messageFrom(payload, res.status));
  return payload;
}

// ----------------------------------------------------------------------------
// Adapters
// ----------------------------------------------------------------------------

// The same formats the app has always shown. Kept identical so switching a page
// to real data cannot be spotted by its date column.
const DATE_FORMAT = { day: '2-digit', month: 'short', year: 'numeric' };
const TIME_FORMAT = { hour: 'numeric', minute: '2-digit' };

const asDate = (ms) => new Date(ms).toLocaleDateString('en-US', DATE_FORMAT);
const asTime = (ms) => new Date(ms).toLocaleTimeString('en-US', TIME_FORMAT);

const withDate = (session) => ({ ...session, date: asDate(session.createdAt) });

// The reading profile lives on the server; the account lives in localStorage.
// Merged rather than replaced so a brand-new signup keeps its own
// `profileCompleted` — that flag is what RouteGuards uses to send someone to
// onboarding, and it is per-account state, which the server has no concept of.
//
// `null` fields are dropped rather than merged. A reader row the server has never
// been told about carries `readerType: null`, and spreading that over the account
// would deselect both Reader Type cards in Settings — the server saying "I was
// never told" is not the same as saying "neither".
function withProfile(user, profile) {
  if (!user) return null;
  const known = Object.fromEntries(Object.entries(profile).filter(([, v]) => v !== null));
  return {
    ...user,
    ...known,
    devicePrefs: { ...user.devicePrefs, ...profile.devicePrefs },
  };
}

async function meFromServer() {
  return request('/me');
}

export const api = {
  // ---------------------------------------------------------------- auth (mock)
  // The password check is local and proves nothing to the server. It exists so
  // the app has a front door, not to protect anything.
  signup: async (details) => withProfile(await mock.apiSignup(details), await meFromServer()),
  login: async (credentials) => withProfile(await mock.apiLogin(credentials), await meFromServer()),
  logout: mock.apiLogout,
  getMe: async () => {
    const user = await mock.apiGetMe();
    return user ? withProfile(user, await meFromServer()) : null;
  },

  // -------------------------------------------------------------- profile setup
  getReadingTestParagraph: () => request('/reading-test'),

  // `elapsedMs` is the whole point of this call: the server measures wpm from the
  // time and its own word count, and rejects a timing it cannot believe with a
  // 422 rather than storing a baseline that would be wrong forever.
  submitReadingTestCompleted: (_userId, elapsedMs) =>
    request('/reading-test', { method: 'POST', body: { elapsedMs } }),

  submitReadingSpeedPreset: (_userId, preset) =>
    request('/reading-speed/preset', { method: 'POST', body: { preset } }),

  submitDevicePreferences: async (_userId, prefs) => {
    const profile = await request('/me', { method: 'PATCH', body: { devicePrefs: prefs } });
    return profile.devicePrefs;
  },

  // Two writes, because the two halves live in two places: the reader type is a
  // reading setting the engine will consult, and `profileCompleted` is onboarding
  // state that belongs to the account. The mock call is what lets RouteGuards
  // stop redirecting to /setup.
  submitReaderType: async (userId, readerType) => {
    const profile = await request('/me', { method: 'PATCH', body: { readerType } });
    return withProfile(await mock.apiSubmitReaderType(userId, readerType), profile);
  },

  // ------------------------------------------------------------------- settings
  updateSettings: async (userId, patch) => {
    const { devicePrefs, readerType, theme, ...account } = patch;
    const profile = await request('/me', {
      method: 'PATCH',
      body: { devicePrefs, readerType, theme },
    });
    return withProfile(await mock.apiUpdateSettings(userId, account), profile);
  },

  // --------------------------------------------------------- dashboard / analysis
  getDashboard: () => request('/dashboard'),

  // Still mock: the charts need weeks of history, and there is one day of it.
  getAnalysis: mock.apiGetAnalysis,

  // ----------------------------------------------------------- sessions / folders
  getSessions: async () => {
    const { folders, sessions } = await request('/sessions');
    return { folders, sessions: sessions.map(withDate) };
  },

  getSessionDetails: async (_userId, sessionId) => {
    const session = await request(`/sessions/${sessionId}`);
    return { ...withDate(session), time: asTime(session.createdAt) };
  },

  createFolder: (_userId, name) => request('/folders', { method: 'POST', body: { name } }),
  renameFolder: (_userId, folderId, name) =>
    request(`/folders/${folderId}`, { method: 'PATCH', body: { name } }),
  deleteFolder: (_userId, folderId) => request(`/folders/${folderId}`, { method: 'DELETE' }),

  renameSession: (_userId, sessionId, name) =>
    request(`/sessions/${sessionId}`, { method: 'PATCH', body: { name } }),
  deleteSession: (_userId, sessionId) => request(`/sessions/${sessionId}`, { method: 'DELETE' }),
  // `folderId: null` means "out of every folder", which is why it is sent
  // explicitly rather than omitted.
  moveSession: (_userId, sessionId, folderId) =>
    request(`/sessions/${sessionId}`, { method: 'PATCH', body: { folderId } }),

  // ------------------------------------------------------- quiz / flashcards (mock)
  // The real data for these is already being written — every finished session
  // stores the flashcards, quiz and words-learned the AI Engine produced. What is
  // missing is the endpoint that merges several sessions' worth, so no new AI call
  // is ever needed. Until then these are the sample banks.
  generateQuiz: mock.apiGenerateQuiz,
  submitQuiz: mock.apiSubmitQuiz,
  generateFlashcards: mock.apiGenerateFlashcards,
};
