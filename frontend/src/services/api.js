// ============================================================================
// TaleTrace — API SERVICE LAYER
// ----------------------------------------------------------------------------
// Pages and components only ever talk to this file, never to the backend
// directly.  That contract is what makes this file the whole integration:
// no page or component below changed when the real backend arrived.
//
// Every call goes through `fetch()` to the FastAPI backend. Authentication is
// handled by an HTTP-only cookie set during signup/login; this file never reads
// or writes it — the browser attaches it automatically.
//
// `userId` is still the first argument at every call site for backward
// compatibility with components written during the mock era. This file drops
// it on the wire; the backend identifies the reader from the cookie.
//
// Shape adapting happens here and only here. The backend speaks in epoch
// milliseconds and its own difficulty vocabulary; the pages want formatted dates
// and Easy/Medium/Hard. Dates are formatted *here*, in the browser, because the
// browser is the only thing in this system that knows the reader's timezone.
// ============================================================================



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
    credentials: 'include',
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
// The charts' x-axis: "Aug 26". Short because a 30-day bar chart has to fit
// thirty of them across a card.
const DAY_FORMAT = { month: 'short', day: 'numeric' };

const asDate = (ms) => new Date(ms).toLocaleDateString('en-US', DATE_FORMAT);
const asTime = (ms) => new Date(ms).toLocaleTimeString('en-US', TIME_FORMAT);
const asDay = (ms) => new Date(ms).toLocaleDateString('en-US', DAY_FORMAT);

const withDate = (session) => ({ ...session, date: asDate(session.createdAt) });

// The five series the Analysis page draws. Each arrives as points carrying a
// `dayStartMs` — the reader's local midnight — and every chart's x-axis reads
// `date`, so the label is added here rather than five times over.
const ANALYSIS_SERIES = [
  'readingTimePerDay',
  'pagesPerDay',
  'lookupsPerDay',
  'speedTrend',
  'difficultyTrend',
];

const withDayLabels = (analysis) => ({
  ...analysis,
  ...Object.fromEntries(
    ANALYSIS_SERIES.map((key) => [
      key,
      analysis[key].map(({ dayStartMs, ...point }) => ({ date: asDay(dayStartMs), ...point })),
    ]),
  ),
});

export const api = {
  // ---------------------------------------------------------------- auth
  signup: async (details) => {
    await request('/auth/signup', { method: 'POST', body: details });
    return request('/me');
  },
  login: async (credentials) => {
    await request('/auth/login', { method: 'POST', body: credentials });
    return request('/me');
  },
  logout: () => request('/auth/logout', { method: 'POST' }),
  getMe: async () => {
    try {
      return await request('/me');
    } catch (e) {
      return null;
    }
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

  submitReaderType: async (_userId, readerType) => {
    return request('/me', { method: 'PATCH', body: { readerType } });
  },

  // ------------------------------------------------------------------- settings
  updateSettings: async (_userId, patch) => {
    return request('/me', {
      method: 'PATCH',
      body: patch,
    });
  },

  getVoices: async () => {
    return request('/voices');
  },

  // --------------------------------------------------------- dashboard / analysis
  getDashboard: () => request('/dashboard'),

  // Real sessions, aggregated per calendar day by the backend. No mock fallback:
  // if this throws, the page shows <ErrorState> and the reader learns the server
  // is down. Quietly serving `mockBackend`'s randomised history instead would put
  // invented reading figures on screen that look exactly like measured ones.
  //
  // `empty: true` comes back for a range with no reading in it, and the arrays are
  // present but empty — the page checks the flag first.
  getAnalysis: async (_userId, range) => {
    const analysis = await request(`/analysis?range=${encodeURIComponent(range)}`);
    return analysis.empty ? analysis : withDayLabels(analysis);
  },

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

  // -------------------------------------------------------------- quiz / flashcards
  // Merged from what each session already stored. The AI Engine wrote the quiz and
  // the cards when the session ended; pressing "Generate" selects and merges rows,
  // and never causes a new AI call.
  //
  // The answer key is not in this file, this bundle, or the browser. `quizId` is
  // what lets the server rebuild it at submit time — see `review.quiz_id`.
  generateQuiz: (_userId, sessionIds) => request('/quiz', { method: 'POST', body: { sessionIds } }),
  submitQuiz: (_userId, quizId, answers) =>
    request('/quiz/submit', { method: 'POST', body: { quizId, answers } }),
  generateFlashcards: (_userId, sessionIds) =>
    request('/flashcards', { method: 'POST', body: { sessionIds } }),
};
