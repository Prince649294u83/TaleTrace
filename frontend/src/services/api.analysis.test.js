// ============================================================================
// The Analysis page's data path, from HTTP to the value a chart plots.
//
//   npm test
//
// The claim under test is narrow and worth stating exactly: every number the
// Analysis page draws comes from the server, `mockBackend` has nothing left to
// fall back to, and a failed request stays failed. That is the whole change —
// the charts, labels and layout are untouched.
//
// Tested here rather than by rendering the page, because the page passes
// `data.speedTrend` straight into <AnalyticsChart> and recharts measures its
// container to decide what to draw: in jsdom that container is 0×0, so a render
// test would assert against an empty SVG and pass whatever the data said. This
// adapter is the last place the values are still readable, and it is the only
// place the mock could have come back.
//
// Not covered: that the chart component then plots what it is handed. That is
// recharts' own contract, and no test here or in the mock era ever checked it.
// ============================================================================

import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from './api';
import * as mock from './mockBackend';

// Local midnight, which is what the backend sends: `dayStartMs`. Built through
// the Date constructor rather than a literal so the expected label is "Aug 20"
// in every timezone — a UTC epoch would be the 19th somewhere.
const AUG_20 = new Date(2026, 7, 20).getTime();
const AUG_21 = new Date(2026, 7, 21).getTime();

// One real response, trimmed to two days. Shape and values copied from
// `GET /api/analysis?range=week` against the seeded corpus.
const RESPONSE = {
  empty: false,
  readingTimePerDay: [
    { dayStartMs: AUG_20, minutes: 3 },
    { dayStartMs: AUG_21, minutes: 5 },
  ],
  pagesPerDay: [
    { dayStartMs: AUG_20, pages: 2 },
    { dayStartMs: AUG_21, pages: 3 },
  ],
  lookupsPerDay: [
    { dayStartMs: AUG_20, lookups: 1 },
    { dayStartMs: AUG_21, lookups: 4 },
  ],
  speedTrend: [
    { dayStartMs: AUG_20, wpm: 180 },
    { dayStartMs: AUG_21, wpm: 165 },
  ],
  difficultyTrend: [
    { dayStartMs: AUG_20, difficulty: 1, difficultyLabel: 'Easy' },
    { dayStartMs: AUG_21, difficulty: 2, difficultyLabel: 'Medium' },
  ],
};

function serve(payload, { status = 200 } = {}) {
  const fetch = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api.getAnalysis', () => {
  it('asks the backend for the selected range', async () => {
    const fetch = serve(RESPONSE);

    await api.getAnalysis('user_demo', 'month');

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe('/api/analysis?range=month');
  });

  it('plots the values the server sent, labelled with the reader’s day', async () => {
    serve(RESPONSE);

    const data = await api.getAnalysis('user_demo', 'week');

    // Exact objects, not property probes: an extra key would mean something was
    // invented on the way through, and `dayStartMs` is expected to be gone —
    // every chart's x-axis reads `date`.
    expect(data.speedTrend).toEqual([
      { date: 'Aug 20', wpm: 180 },
      { date: 'Aug 21', wpm: 165 },
    ]);
    expect(data.readingTimePerDay).toEqual([
      { date: 'Aug 20', minutes: 3 },
      { date: 'Aug 21', minutes: 5 },
    ]);
    expect(data.pagesPerDay).toEqual([
      { date: 'Aug 20', pages: 2 },
      { date: 'Aug 21', pages: 3 },
    ]);
    expect(data.lookupsPerDay).toEqual([
      { date: 'Aug 20', lookups: 1 },
      { date: 'Aug 21', lookups: 4 },
    ]);
    expect(data.difficultyTrend).toEqual([
      { date: 'Aug 20', difficulty: 1, difficultyLabel: 'Easy' },
      { date: 'Aug 21', difficulty: 2, difficultyLabel: 'Medium' },
    ]);
    expect(data.empty).toBe(false);
  });

  it('keeps a null as a null', async () => {
    // A day whose sessions were all too short to time, or all unrated. The chart
    // draws a gap; a 0 would claim the reader read at zero words a minute.
    serve({
      ...RESPONSE,
      speedTrend: [{ dayStartMs: AUG_20, wpm: null }],
      difficultyTrend: [{ dayStartMs: AUG_20, difficulty: null, difficultyLabel: null }],
    });

    const data = await api.getAnalysis('user_demo', 'week');

    expect(data.speedTrend).toEqual([{ date: 'Aug 20', wpm: null }]);
    expect(data.difficultyTrend[0].difficultyLabel).toBeNull();
  });

  it('passes an empty range through untouched', async () => {
    const empty = {
      empty: true,
      readingTimePerDay: [],
      pagesPerDay: [],
      lookupsPerDay: [],
      speedTrend: [],
      difficultyTrend: [],
    };
    serve(empty);

    expect(await api.getAnalysis('user_demo', 'today')).toEqual(empty);
  });

  it('lets a failed request fail', async () => {
    // The one behaviour the page depends on. Falling back to a local dataset
    // would put invented reading figures on screen that look exactly like
    // measured ones, and nobody could tell by looking which they were seeing.
    serve({ detail: 'Database is locked' }, { status: 500 });

    await expect(api.getAnalysis('user_demo', 'week')).rejects.toThrow('Database is locked');
  });
});

describe('mockBackend', () => {
  it('has no analysis implementation left to fall back to', () => {
    // Deleted, not merely unreferenced. An unused function that still returns
    // randomised charts is one `catch` block away from being used again.
    expect(Object.keys(mock).filter((name) => /analysis/i.test(name))).toEqual([]);
  });

  it('seeds an account and no reading history', async () => {
    // `seedDemoAccount` used to fabricate seven sessions with `Math.random()`
    // pages, lookups, WPM and difficulty. The login it provides is all that is
    // left of it, and the sessions it creates must stay at zero.
    const store = new Map();
    vi.stubGlobal('localStorage', {
      getItem: (key) => store.get(key) ?? null,
      setItem: (key, value) => store.set(key, value),
    });

    await mock.apiLogin({ email: 'demo@taletrace.app', password: 'demo1234' });

    const db = JSON.parse(store.get('taletrace_db_v1'));
    expect(db.users).toHaveLength(1);
    expect(db.sessions).toEqual([]);
    expect(db.folders).toEqual([]);
  });
});
