// ============================================================================
// TaleTrace — MOCK BACKEND
// ----------------------------------------------------------------------------
// This file stands in for the real TaleTrace backend so the frontend MVP is
// fully demoable without a server. Every "business logic" concern the brief
// assigns to the backend (WPM calculation, difficulty, analytics, quiz &
// flashcard generation, statistics) happens ONLY in this file.
//
// The rest of the app (services/api.js, pages, components) only ever reads
// the JSON this module returns. If a real backend is wired up later, only
// services/api.js needs to change — nothing else in the app should need to.
// ============================================================================

const DB_KEY = 'taletrace_db_v1';
const NET_DELAY = 420;

const uid = (prefix = 'id') =>
  `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;

const delay = (ms = NET_DELAY) => new Promise((res) => setTimeout(res, ms));

function loadDb() {
  const raw = localStorage.getItem(DB_KEY);
  if (raw) return JSON.parse(raw);
  const fresh = { users: [], sessions: [], folders: [], currentUserId: null };
  localStorage.setItem(DB_KEY, JSON.stringify(fresh));
  return fresh;
}

function saveDb(db) {
  localStorage.setItem(DB_KEY, JSON.stringify(db));
}

// ----------------------------------------------------------------------------
// Demo seed data — gives the app something to show immediately for the
// built-in demo account, without affecting brand-new signups (which
// correctly start on empty states, per spec).
// ----------------------------------------------------------------------------
function seedDemoAccount(db) {
  const userId = 'user_demo';
  if (db.users.find((u) => u.id === userId)) return;

  db.users.push({
    id: userId,
    name: 'Prince',
    email: 'demo@taletrace.app',
    password: 'demo1234',
    age: 24,
    profileCompleted: true,
    wpm: 245,
    readingSpeedPreset: null,
    devicePrefs: {
      textToSpeech: true,
      ambientMusic: false,
      readOutMeaning: true,
      displayMeaning: true,
    },
    readerType: 'normal',
    theme: 'dark',
    deviceStatus: 'connected',
    createdAt: Date.now(),
  });

  const folders = [
    { id: uid('folder'), userId, name: 'Biology' },
    { id: uid('folder'), userId, name: 'English' },
  ];
  db.folders.push(...folders);

  const difficulties = ['Easy', 'Medium', 'Hard'];
  const titles = [
    ['Biology', 'Chapter 1 — Cell Structure'],
    ['Biology', 'Chapter 2 — Photosynthesis'],
    ['Biology', 'Chapter 3 — Genetics'],
    ['English', 'First Flight — Ch. 4'],
    ['English', 'The Last Lesson'],
    [null, 'History — The Industrial Age'],
    [null, 'Reading Session'],
  ];

  titles.forEach(([folderName, name], i) => {
    const folder = folders.find((f) => f.name === folderName);
    const daysAgo = i;
    const date = Date.now() - daysAgo * 86400000;
    db.sessions.push({
      id: uid('session'),
      userId,
      folderId: folder ? folder.id : null,
      name,
      createdAt: date,
      readingTimeMin: 25 + Math.floor(Math.random() * 30),
      pagesRead: 12 + Math.floor(Math.random() * 25),
      lookups: Math.floor(Math.random() * 12),
      wpm: 220 + Math.floor(Math.random() * 60),
      difficulty: difficulties[Math.floor(Math.random() * difficulties.length)],
      summary:
        'This section walks through the core ideas of the chapter, connecting each concept back to the central theme and highlighting the terms most likely to appear in review. Key examples are worked through step by step, and the closing passage ties the material back to the broader unit.',
    });
  });

  saveDb(db);
}

function requireUser(db, userId) {
  const user = db.users.find((u) => u.id === userId);
  if (!user) throw new Error('Not authenticated');
  return user;
}

// ============================================================================
// AUTH
// ============================================================================

export async function apiSignup({ name, email, age, password }) {
  await delay();
  const db = loadDb();
  seedDemoAccount(db);

  if (db.users.some((u) => u.email.toLowerCase() === email.toLowerCase())) {
    throw new Error('An account with this email already exists.');
  }

  const user = {
    id: uid('user'),
    name,
    email,
    age: Number(age) || null,
    password,
    profileCompleted: false,
    wpm: null,
    readingSpeedPreset: null,
    devicePrefs: {
      textToSpeech: true,
      ambientMusic: false,
      readOutMeaning: true,
      displayMeaning: true,
    },
    readerType: null,
    theme: 'dark',
    deviceStatus: 'disconnected',
    createdAt: Date.now(),
  };
  db.users.push(user);
  db.currentUserId = user.id;
  saveDb(db);
  return sanitize(user);
}

export async function apiLogin({ email, password }) {
  await delay();
  const db = loadDb();
  seedDemoAccount(db);

  const user = db.users.find((u) => u.email.toLowerCase() === email.toLowerCase());
  if (!user || user.password !== password) {
    throw new Error('Incorrect email or password.');
  }
  db.currentUserId = user.id;
  saveDb(db);
  return sanitize(user);
}

export async function apiLogout() {
  await delay(150);
  const db = loadDb();
  db.currentUserId = null;
  saveDb(db);
}

export async function apiGetMe() {
  await delay(250);
  const db = loadDb();
  if (!db.currentUserId) return null;
  const user = db.users.find((u) => u.id === db.currentUserId);
  return user ? sanitize(user) : null;
}

function sanitize(user) {
  // eslint-disable-next-line no-unused-vars
  const { password: _password, ...rest } = user;
  return rest;
}

// ============================================================================
// PROFILE SETUP
// ============================================================================

const SETUP_PARAGRAPH =
  "The lighthouse keeper climbed the spiral stairs before dawn, counting each step out of habit rather than need. Fog had rolled in overnight, thick enough to swallow the shoreline whole, and the beam above would matter more than usual. He had done this for eleven years, yet the climb never felt routine — every morning carried its own small weather, its own reason to pay attention.";

export async function apiGetReadingTestParagraph() {
  await delay(300);
  return { paragraph: SETUP_PARAGRAPH };
}

export async function apiSubmitReadingSpeedPreset(userId, preset) {
  await delay();
  const db = loadDb();
  const user = requireUser(db, userId);
  const presetToWpm = { '1x': 200, '1.5x': 260, '2x': 320 };
  user.readingSpeedPreset = preset;
  user.wpm = presetToWpm[preset] || 220;
  saveDb(db);
  return { wpm: user.wpm };
}

export async function apiSubmitReadingTestCompleted(userId) {
  await delay(600);
  const db = loadDb();
  const user = requireUser(db, userId);
  // Backend "calculates" WPM from the test — frontend never computes this.
  const wpm = 210 + Math.floor(Math.random() * 90);
  user.wpm = wpm;
  user.readingSpeedPreset = null;
  saveDb(db);
  return { wpm };
}

export async function apiSubmitDevicePreferences(userId, prefs) {
  await delay();
  const db = loadDb();
  const user = requireUser(db, userId);
  user.devicePrefs = { ...user.devicePrefs, ...prefs };
  saveDb(db);
  return user.devicePrefs;
}

export async function apiSubmitReaderType(userId, readerType) {
  await delay();
  const db = loadDb();
  const user = requireUser(db, userId);
  user.readerType = readerType;
  user.profileCompleted = true;
  saveDb(db);
  return sanitize(user);
}

// ============================================================================
// SETTINGS
// ============================================================================

export async function apiUpdateSettings(userId, patch) {
  await delay();
  const db = loadDb();
  const user = requireUser(db, userId);
  Object.assign(user, patch);
  if (patch.devicePrefs) user.devicePrefs = { ...user.devicePrefs, ...patch.devicePrefs };
  saveDb(db);
  return sanitize(user);
}

// ============================================================================
// DASHBOARD
// ============================================================================

export async function apiGetDashboard(userId) {
  await delay();
  const db = loadDb();
  const user = requireUser(db, userId);
  const sessions = db.sessions.filter((s) => s.userId === userId);

  if (sessions.length === 0) {
    return {
      empty: true,
      username: user.name,
      deviceStatus: user.deviceStatus,
      wpm: user.wpm,
    };
  }

  const today = new Date().toDateString();
  const todays = sessions.filter((s) => new Date(s.createdAt).toDateString() === today);
  const pagesToday = todays.reduce((sum, s) => sum + s.pagesRead, 0);
  const lookupsToday = todays.reduce((sum, s) => sum + s.lookups, 0);

  return {
    empty: false,
    username: user.name,
    deviceStatus: user.deviceStatus,
    wpm: user.wpm,
    pagesToday,
    lookupsToday,
  };
}

// ============================================================================
// ANALYSIS
// ============================================================================

function rangeToDays(range) {
  if (range === 'today') return 1;
  if (range === 'week') return 7;
  if (range === 'month') return 30;
  return 90; // "all time" cap for the demo
}

export async function apiGetAnalysis(userId, range) {
  await delay(550);
  const db = loadDb();
  const sessions = db.sessions.filter((s) => s.userId === userId);
  const days = rangeToDays(range);

  if (sessions.length === 0) {
    return { empty: true };
  }

  const byDay = {};
  const cutoff = Date.now() - days * 86400000;
  sessions
    .filter((s) => s.createdAt >= cutoff)
    .forEach((s) => {
      const key = new Date(s.createdAt).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
      });
      if (!byDay[key]) {
        byDay[key] = {
          date: key,
          readingTime: 0,
          pages: 0,
          lookups: 0,
          wpmSamples: [],
          difficultySamples: [],
          ts: s.createdAt,
        };
      }
      byDay[key].readingTime += s.readingTimeMin;
      byDay[key].pages += s.pagesRead;
      byDay[key].lookups += s.lookups;
      byDay[key].wpmSamples.push(s.wpm);
      byDay[key].difficultySamples.push(s.difficulty);
    });

  const difficultyScore = { Easy: 1, Medium: 2, Hard: 3 };
  const scoreDifficulty = { 1: 'Easy', 2: 'Medium', 3: 'Hard' };

  const days_ = Object.values(byDay).sort((a, b) => a.ts - b.ts);

  const readingTimePerDay = days_.map((d) => ({ date: d.date, minutes: d.readingTime }));
  const pagesPerDay = days_.map((d) => ({ date: d.date, pages: d.pages }));
  const lookupsPerDay = days_.map((d) => ({ date: d.date, lookups: d.lookups }));
  const speedTrend = days_.map((d) => ({
    date: d.date,
    wpm: Math.round(d.wpmSamples.reduce((a, b) => a + b, 0) / d.wpmSamples.length),
  }));
  const difficultyTrend = days_.map((d) => {
    const avg = Math.round(
      d.difficultySamples.reduce((a, s) => a + difficultyScore[s], 0) / d.difficultySamples.length
    );
    return { date: d.date, difficulty: avg, difficultyLabel: scoreDifficulty[avg] || 'Medium' };
  });

  return {
    empty: false,
    readingTimePerDay,
    pagesPerDay,
    lookupsPerDay,
    speedTrend,
    difficultyTrend,
  };
}

// ============================================================================
// SESSIONS / FOLDERS
// ============================================================================

export async function apiGetSessions(userId) {
  await delay();
  const db = loadDb();
  const folders = db.folders.filter((f) => f.userId === userId);
  const sessions = db.sessions
    .filter((s) => s.userId === userId)
    .sort((a, b) => b.createdAt - a.createdAt)
    .map((s) => ({
      id: s.id,
      name: s.name,
      folderId: s.folderId,
      date: new Date(s.createdAt).toLocaleDateString('en-US', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
      }),
      createdAt: s.createdAt,
    }));
  return { folders, sessions };
}

export async function apiCreateFolder(userId, name) {
  await delay(300);
  const db = loadDb();
  const folder = { id: uid('folder'), userId, name };
  db.folders.push(folder);
  saveDb(db);
  return folder;
}

export async function apiRenameFolder(userId, folderId, name) {
  await delay(300);
  const db = loadDb();
  const folder = db.folders.find((f) => f.id === folderId && f.userId === userId);
  if (folder) folder.name = name;
  saveDb(db);
  return folder;
}

export async function apiDeleteFolder(userId, folderId) {
  await delay(300);
  const db = loadDb();
  db.folders = db.folders.filter((f) => !(f.id === folderId && f.userId === userId));
  db.sessions.forEach((s) => {
    if (s.folderId === folderId && s.userId === userId) s.folderId = null;
  });
  saveDb(db);
}

export async function apiRenameSession(userId, sessionId, name) {
  await delay(300);
  const db = loadDb();
  const session = db.sessions.find((s) => s.id === sessionId && s.userId === userId);
  if (session) session.name = name;
  saveDb(db);
  return session;
}

export async function apiDeleteSession(userId, sessionId) {
  await delay(300);
  const db = loadDb();
  db.sessions = db.sessions.filter((s) => !(s.id === sessionId && s.userId === userId));
  saveDb(db);
}

export async function apiMoveSession(userId, sessionId, folderId) {
  await delay(250);
  const db = loadDb();
  const session = db.sessions.find((s) => s.id === sessionId && s.userId === userId);
  if (session) session.folderId = folderId;
  saveDb(db);
  return session;
}

export async function apiGetSessionDetails(userId, sessionId) {
  await delay();
  const db = loadDb();
  const session = db.sessions.find((s) => s.id === sessionId && s.userId === userId);
  if (!session) throw new Error('Session not found.');
  return {
    id: session.id,
    name: session.name,
    date: new Date(session.createdAt).toLocaleDateString('en-US', {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    }),
    time: new Date(session.createdAt).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    }),
    readingTimeMin: session.readingTimeMin,
    pagesRead: session.pagesRead,
    difficulty: session.difficulty,
    summary: session.summary,
  };
}

// ============================================================================
// QUIZZES
// ============================================================================

const QUIZ_BANK = [
  {
    q: 'What is the primary pigment involved in capturing light energy during photosynthesis?',
    options: ['Chlorophyll', 'Melanin', 'Keratin', 'Hemoglobin'],
    answer: 0,
  },
  {
    q: 'Which organelle is responsible for producing most of a cell\u2019s ATP?',
    options: ['Golgi apparatus', 'Mitochondrion', 'Ribosome', 'Lysosome'],
    answer: 1,
  },
  {
    q: 'In genetics, what term describes the different forms of a gene?',
    options: ['Genotype', 'Phenotype', 'Alleles', 'Chromatid'],
    answer: 2,
  },
  {
    q: 'What best describes the central theme of the reading?',
    options: [
      'A technical process explained step by step',
      'An unrelated historical anecdote',
      'A list of unconnected facts',
      'A personal opinion with no evidence',
    ],
    answer: 0,
  },
  {
    q: 'Which detail from the chapter is most likely to appear on a follow-up quiz?',
    options: ['A minor aside', 'A key term defined in the text', 'A footnote', 'An illustration caption'],
    answer: 1,
  },
  {
    q: 'What was the main driver of change discussed in the Industrial Age reading?',
    options: ['Agricultural decline', 'Mechanization and new energy sources', 'Population decrease', 'Trade embargoes'],
    answer: 1,
  },
];

// In-memory answer key store, keyed by quizId. The frontend is only ever
// given the questions/options — never the correct answers — so that scoring
// (a business-logic calculation) stays on the "backend".
const quizAnswerKeys = new Map();

export async function apiGenerateQuiz(userId, sessionIds) {
  await delay(1100);
  if (!sessionIds || sessionIds.length === 0) {
    throw new Error('Select at least one session to generate a quiz.');
  }
  const count = Math.min(10, Math.max(5, sessionIds.length * 2));
  const quizId = uid('quiz');
  const answerKey = {};
  const questions = Array.from({ length: count }, (_, i) => {
    const item = QUIZ_BANK[i % QUIZ_BANK.length];
    const id = uid('q');
    answerKey[id] = item.answer;
    return { id, question: item.q, options: item.options };
  });
  quizAnswerKeys.set(quizId, answerKey);
  return { quizId, questions };
}

export async function apiSubmitQuiz(userId, quizId, answers) {
  await delay(500);
  const answerKey = quizAnswerKeys.get(quizId) || {};
  let correctCount = 0;
  const results = Object.entries(answers).map(([questionId, selectedIndex]) => {
    const correctIndex = answerKey[questionId];
    const correct = selectedIndex === correctIndex;
    if (correct) correctCount += 1;
    return { questionId, correct, correctIndex };
  });
  const total = Object.keys(answerKey).length;
  return { score: correctCount, total, percent: Math.round((correctCount / total) * 100), results };
}

// ============================================================================
// FLASHCARDS
// ============================================================================

const FLASHCARD_BANK = [
  { term: 'Photosynthesis', definition: 'The process by which plants convert light energy into chemical energy stored in glucose.' },
  { term: 'Mitochondrion', definition: 'The organelle responsible for generating most of a cell\u2019s supply of ATP.' },
  { term: 'Allele', definition: 'One of two or more alternative forms of a gene that arise by mutation.' },
  { term: 'Chlorophyll', definition: 'The green pigment in plants that absorbs light energy for photosynthesis.' },
  { term: 'Genotype', definition: 'The genetic makeup of an organism, as distinguished from its physical characteristics.' },
  { term: 'Industrialization', definition: 'The transformation of an economy from primarily agricultural to one based on manufacturing.' },
  { term: 'Mechanization', definition: 'The process of changing from working largely by hand to using machines.' },
  { term: 'Cell membrane', definition: 'The semi-permeable barrier that separates the inside of a cell from its surroundings.' },
];

export async function apiGenerateFlashcards(userId, sessionIds) {
  await delay(1000);
  if (!sessionIds || sessionIds.length === 0) {
    throw new Error('Select at least one session to generate flashcards.');
  }
  const count = Math.min(12, Math.max(6, sessionIds.length * 2));
  const cards = Array.from({ length: count }, (_, i) => {
    const item = FLASHCARD_BANK[i % FLASHCARD_BANK.length];
    return { id: uid('card'), term: item.term, definition: item.definition };
  });
  return { cards };
}
