"""Quizzes and flashcards, merged out of what the AI Engine already wrote.

No AI call happens in this module, and none may be added to it. Every finished
session already stores the flashcards, quiz, words-learned and summary that
`AiBridge.review()` produced at the moment the reader put the book down —
`sessions.review_payload` is that blob, stored whole. Opening the Quizzes page is
a *read* of work already done and paid for. Calling Groq again because somebody
clicked "Generate" would spend a second request to produce a different quiz about
the same reading, and the reader would have no way to tell why the questions
changed.

    review_payload → parse → validate → merge → cap → the page

What this module distrusts
--------------------------
The payload came from a language model answering a JSON schema, and a model is
capable of returning a quiz whose `correct_answer` is not one of its own
`options`, a flashcard with a word and no definition, or four options of which
two are the same string. Every one of those has to be dropped *here*, because the
alternative is a React component deciding what a broken question means, and the
frontend is the one place in this system that must never have to interpret an
inconsistent payload. A question that cannot be scored is not shown; a card with
nothing on its back is not dealt.

Dropping is silent by design. The payload is not editable and a reader cannot act
on "one of your six questions was malformed", so surfacing it would be noise. The
count on screen is the count of questions that work.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from backend.app.modules.database.models import Session as SessionRow

# A ten-question ceiling, so selecting a month of reading cannot produce a
# hundred-question quiz nobody will finish. Ten is the mock's old maximum, which
# is what the page was designed around.
MAX_QUIZ_QUESTIONS = 10

# Fewer than two options is not a multiple-choice question. The prompt asks for
# four; two is the floor at which the reader still has a choice to get wrong, and
# accepting three keeps a question the model shortened rather than throwing away
# the only quiz a session has.
MIN_QUIZ_OPTIONS = 2

# Session ids are `str(uuid4())` — hex and hyphens, never a dot. See `quiz_id`.
QUIZ_ID_SEPARATOR = "."

_SPACES = re.compile(r"\s+")
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$", re.UNICODE)


@dataclass(frozen=True)
class Flashcard:
    """One card. `term` on the front, `definition` on the back."""

    id: str
    term: str
    definition: str


@dataclass(frozen=True)
class Question:
    """One scoreable multiple-choice question.

    `correct_index` never leaves this process. It is resolved from the payload's
    `correct_answer` *string* at parse time so that scoring is an integer
    comparison later, and so a question whose answer is not among its options is
    rejected once, here, rather than mis-scored as option zero.
    """

    id: str
    question: str
    options: tuple[str, ...]
    correct_index: int


# ------------------------------------------------------------------- normalising


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The list under `key` in a session's review payload, or an empty list.

    Defensive at every level: the payload may be `{}` for a session read without
    Meaning Mode, the key may be absent, and the value may be a string where a
    list was asked for. None of those is an error — they mean "no quiz here".
    """

    value = (payload or {}).get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str | None:
    """A usable string, or `None`. Whitespace-only and non-strings are `None`.

    Inner runs of whitespace collapse, because a model that wrapped a question
    across two lines wrote the same question as one that did not.
    """

    if not isinstance(value, str):
        return None
    return _SPACES.sub(" ", value).strip() or None


def _key(text: str) -> str:
    """The dedup key for a word or a question.

    Case, surrounding punctuation and inner spacing are all ignored, so
    "Chlorophyll", "chlorophyll" and "chlorophyll," are one word, and the same
    question asked with and without its final question mark is one question.
    `casefold` rather than `lower` so that non-English vocabulary the reader looked
    up dedups too.
    """

    return _EDGE_PUNCTUATION.sub("", _SPACES.sub(" ", text).strip()).casefold()


def _ordered(rows: Iterable[SessionRow]) -> list[SessionRow]:
    """Oldest first, ties broken by id.

    Deterministic ordering is the whole basis of `quiz_id`: the same selection has
    to rebuild the same quiz at submit time as it served at generate time, in a
    different request, possibly in a different process. Sorting on `created_at`
    alone is not enough — two sessions seeded in the same second would swap.
    """

    return sorted(rows, key=lambda row: (row.created_at, row.id))


# ----------------------------------------------------------------- one session


def _vocabulary(payload: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """`words_learned` as `{key: (word, takeaway)}`, first spelling winning."""

    found: dict[str, tuple[str, str]] = {}
    for item in _items(payload, "words_learned"):
        word = _text(item.get("word"))
        takeaway = _text(item.get("takeaway"))
        if word and takeaway:
            found.setdefault(_key(word), (word, takeaway))
    return found


def validate_learning_material(raw_payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Sanitise LLM output for quizzes and flashcards into trusted learning material.
    
    Enforces all Phase 1 rules: deduplication, limits, valid answer presence.
    Returns a dict with 'quiz' and 'flashcards' containing only valid items.
    """
    vocabulary = _vocabulary(raw_payload)
    
    # 1. Validate Flashcards
    valid_flashcards: list[dict[str, Any]] = []
    seen_cards: set[str] = set()

    for item in _items(raw_payload, "flashcards"):
        word = _text(item.get("word"))
        if word is None:
            continue
        key = _key(word)
        definition = (
            _text(item.get("fun_definition"))
            or _text(item.get("hint_from_story"))
            or (vocabulary[key][1] if key in vocabulary else None)
        )
        if definition is None or key in seen_cards:
            continue
        seen_cards.add(key)
        valid_flashcards.append({
            "word": word,
            "hint_from_story": _text(item.get("hint_from_story")) or "",
            "fun_definition": definition,
        })

    # Add fallback flashcards for vocabulary not yet covered
    for key, (word, takeaway) in vocabulary.items():
        if key not in seen_cards:
            valid_flashcards.append({
                "word": word,
                "hint_from_story": "",
                "fun_definition": takeaway,
            })
            seen_cards.add(key)

    # 2. Validate Quiz
    valid_quiz: list[dict[str, Any]] = []
    seen_questions: set[str] = set()

    for item in _items(raw_payload, "quiz"):
        question = _text(item.get("question"))
        answer = _text(item.get("correct_answer"))
        raw_options = item.get("options")
        if question is None or answer is None or not isinstance(raw_options, list):
            continue

        options = [text for option in raw_options if (text := _text(option)) is not None]
        keys = [_key(option) for option in options]
        if len(options) < MIN_QUIZ_OPTIONS or len(set(keys)) != len(keys):
            continue

        correct = [position for position, key in enumerate(keys) if key == _key(answer)]
        if len(correct) != 1:
            continue

        key = _key(question)
        if key in seen_questions:
            continue
        seen_questions.add(key)
        
        valid_quiz.append({
            "question": question,
            "options": options,
            "correct_answer": options[correct[0]],
            "feedback": _text(item.get("feedback")) or "",
        })

    return {
        "flashcards": valid_flashcards,
        "quiz": valid_quiz,
    }


def flashcards_in(row: SessionRow) -> list[Flashcard]:
    """Every usable card for one session, in the order the model wrote them."""
    payload = row.review_payload or {}
    validated = validate_learning_material(payload)
    cards: list[Flashcard] = []
    for index, item in enumerate(validated["flashcards"]):
        cards.append(Flashcard(
            id=f"{row.id}#c{index}", 
            term=item["word"], 
            definition=item["fun_definition"]
        ))
    return cards


def quiz_in(row: SessionRow) -> list[Question]:
    """Every scoreable question for one session, in the order the model wrote them."""
    payload = row.review_payload or {}
    validated = validate_learning_material(payload)
    questions: list[Question] = []
    for index, item in enumerate(validated["quiz"]):
        options = item["options"]
        correct_index = options.index(item["correct_answer"])
        questions.append(Question(
            id=f"{row.id}#q{index}",
            question=item["question"],
            options=tuple(options),
            correct_index=correct_index,
        ))
    return questions


# -------------------------------------------------------------- several sessions


def merge_flashcards(rows: Iterable[SessionRow]) -> list[Flashcard]:
    """One deck from several sessions, one card per distinct word.

    Deliberately uncapped, unlike the quiz. A quiz is a sitting with an end; a
    deck is a reference the reader flicks through, and every card in it is a word
    they stopped reading to ask about. Dropping some to hit a round number would
    hide vocabulary for no reason the reader could see — the same argument that
    keeps a short session in the analytics.
    """

    deck: list[Flashcard] = []
    seen: set[str] = set()
    for row in _ordered(rows):
        for card in flashcards_in(row):
            key = _key(card.term)
            if key not in seen:
                seen.add(key)
                deck.append(card)
    return deck


def merge_quiz(
    rows: Iterable[SessionRow], *, limit: int = MAX_QUIZ_QUESTIONS
) -> list[Question]:
    """One quiz from several sessions, capped at `limit` questions.

    Round-robin across the sessions rather than in order: the first session's
    questions taken first would fill a ten-question quiz from one afternoon's
    reading when the reader selected five, and they selected five because they
    wanted to be asked about all of them. So each session contributes its first
    question, then its second, until the cap.
    """

    per_session = [quiz_in(row) for row in _ordered(rows)]
    quiz: list[Question] = []
    seen: set[str] = set()

    for depth in range(max((len(questions) for questions in per_session), default=0)):
        for questions in per_session:
            if depth >= len(questions):
                continue
            key = _key(questions[depth].question)
            if key in seen:
                continue
            seen.add(key)
            quiz.append(questions[depth])
            if len(quiz) >= limit:
                return quiz

    return quiz


# ------------------------------------------------------------------ quiz identity


def quiz_id(session_ids: Iterable[str]) -> str:
    """The identifier for the quiz these sessions produce.

    The id *is* the storage. There is no quizzes table and no in-memory map of
    answer keys, because `merge_quiz` over the same rows is deterministic: the
    same sessions rebuild the same questions in the same order with the same
    correct indices, so submit can re-derive the key that generate never sent.

    That buys three things a map would not. It survives a restart, so `--reload`
    firing mid-quiz does not lose the answers. It cannot leak, since there is
    nothing to expire or evict. And the answer key still never reaches the
    browser — the only thing the browser holds is a list of session ids it chose
    itself.

    Sorted and de-duplicated so that selecting the same three sessions in a
    different order is the same quiz.
    """

    return QUIZ_ID_SEPARATOR.join(sorted(set(session_ids)))


def sessions_in(quiz: str) -> list[str]:
    """The session ids inside a quiz id."""

    return [part for part in quiz.split(QUIZ_ID_SEPARATOR) if part]


# ------------------------------------------------------------------------ scoring


def score(questions: Sequence[Question], answers: dict[str, int]) -> dict[str, Any]:
    """Mark a submission against the quiz it was generated from.

    Iterates the *quiz*, not the submission, so `total` is the number of questions
    asked. An unanswered question is wrong, which is the same thing it would be on
    paper, and an answer for a question this quiz does not contain earns nothing
    rather than being an error — a stale tab resubmitting is not worth a 400 when
    the score it gets is already correct.

    Requires a non-empty quiz; a quiz with no questions is never served, so
    reaching here with one means a caller skipped that check.
    """

    results = [
        {
            "questionId": question.id,
            "correct": answers.get(question.id) == question.correct_index,
            "correctIndex": question.correct_index,
        }
        for question in questions
    ]
    correct = sum(1 for result in results if result["correct"])
    return {
        "score": correct,
        "total": len(questions),
        "percent": round(correct / len(questions) * 100),
        "results": results,
    }
