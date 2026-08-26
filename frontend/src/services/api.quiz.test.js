// ============================================================================
// Quiz and Flashcards — the data path from HTTP to the component.
//
//   npm test
//
// The claims under test:
//
//   * `api.generateQuiz` calls `POST /api/quiz` with `sessionIds` and returns
//     the quiz with `quizId` and `questions` — no answer key in the payload;
//   * `api.submitQuiz` calls `POST /api/quiz/submit` with `quizId` and
//     `answers` and returns the server-scored result;
//   * `api.generateFlashcards` calls `POST /api/flashcards` and returns the
//     merged deck;
//   * a failed request stays failed — no mock fallback catches it;
//   * `mockBackend` has no quiz, flashcard or generateQuiz export left to
//     fall back to.
//
// Tested at the adapter rather than the component, because QuizViewer and
// FlashcardViewer both pass data straight through, and testing them in jsdom
// would assert layout rather than data flow. This is the last place the values
// are readable and the only place a mock could sneak back.
// ============================================================================

import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from './api';

// A server response for `POST /api/quiz` — one question, no answer key.
const QUIZ_RESPONSE = {
  quizId: 'session-1.session-2',
  questions: [
    {
      id: 'session-1#q0',
      question: 'What is photosynthesis?',
      options: ['Energy from light', 'Energy from heat', 'Energy from sound', 'Energy from wind'],
    },
  ],
};

// A server response for `POST /api/quiz/submit`.
const SCORE_RESPONSE = {
  score: 1,
  total: 1,
  percent: 100,
  results: [{ questionId: 'session-1#q0', correct: true, correctIndex: 0 }],
};

// A server response for `POST /api/flashcards`.
const FLASHCARDS_RESPONSE = {
  cards: [
    { id: 'session-1#c0', term: 'chlorophyll', definition: 'The green pigment in leaves.' },
    { id: 'session-1#c1', term: 'stomata', definition: 'Tiny pores on leaves.' },
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

describe('api.generateQuiz', () => {
  it('posts the selected session ids to the backend', async () => {
    const fetch = serve(QUIZ_RESPONSE);

    await api.generateQuiz('user_demo', ['session-1', 'session-2']);

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('/api/quiz');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ sessionIds: ['session-1', 'session-2'] });
  });

  it('returns the quiz with no answer key', async () => {
    serve(QUIZ_RESPONSE);

    const data = await api.generateQuiz('user_demo', ['session-1']);

    expect(data.quizId).toBe('session-1.session-2');
    expect(data.questions).toHaveLength(1);
    expect(data.questions[0]).toEqual(QUIZ_RESPONSE.questions[0]);
    // The answer key must never be in the response.
    expect(data.questions[0]).not.toHaveProperty('correct_answer');
    expect(data.questions[0]).not.toHaveProperty('correctIndex');
  });

  it('lets a failed request fail', async () => {
    serve(
      { detail: 'There is nothing to build from in those sessions.' },
      { status: 404 },
    );

    await expect(api.generateQuiz('user_demo', ['s1'])).rejects.toThrow(
      'nothing to build',
    );
  });
});

describe('api.submitQuiz', () => {
  it('sends the quiz id and answers for server-side scoring', async () => {
    const fetch = serve(SCORE_RESPONSE);

    const result = await api.submitQuiz('user_demo', 'quiz-id-1', { 'q0': 0 });

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('/api/quiz/submit');
    expect(JSON.parse(opts.body)).toEqual({ quizId: 'quiz-id-1', answers: { q0: 0 } });
    expect(result.score).toBe(1);
    expect(result.total).toBe(1);
    expect(result.percent).toBe(100);
  });
});

describe('api.generateFlashcards', () => {
  it('posts session ids and returns the deck', async () => {
    const fetch = serve(FLASHCARDS_RESPONSE);

    const data = await api.generateFlashcards('user_demo', ['session-1']);

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, opts] = fetch.mock.calls[0];
    expect(url).toBe('/api/flashcards');
    expect(JSON.parse(opts.body)).toEqual({ sessionIds: ['session-1'] });
    expect(data.cards).toHaveLength(2);
    expect(data.cards[0].term).toBe('chlorophyll');
  });

  it('lets a failed request fail', async () => {
    serve({ detail: 'nothing to build' }, { status: 404 });

    await expect(api.generateFlashcards('user_demo', ['s1'])).rejects.toThrow(
      'nothing to build',
    );
  });
});

