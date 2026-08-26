"""Companion API tests — the website's contract with the reading engine.

No hardware, no network, no AI. Every test writes a session row the way
`live_session` does and then reads it back the way the website does, because the
mistakes worth catching here are all mistranslations between those two ends.

The claims, one per mistake that would be invisible on screen:

  * an implausible reading-test timing is refused, not clamped — a stopwatch left
    running would otherwise become a permanent wrong baseline that every later
    difficulty verdict is measured against;
  * an unrated session serialises as no difficulty at all, never as "Medium" —
    UNKNOWN means the engine declined to rate, and rendering it as the middle
    rating would invent a measurement;
  * a session read without Meaning Mode has no summary and still loads — the AI
    Engine refuses to summarise nothing, which is correct, and must not read as a
    server error;
  * deleting a folder unfiles its sessions rather than taking them with it;
  * each Analysis time filter selects its own window, an empty window says so
    instead of drawing zeroes, and neither sentinel — an unmeasurable pace or an
    unrated page — is ever averaged into a day.

The database is a fresh in-memory SQLite per test, wired in by overriding the
`get_db` dependency. `StaticPool` because the default pool would hand each
connection its own private in-memory database, and a row written by one would be
invisible to the next.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.app.api.auth import get_current_reader
from backend.app.api.companion import READING_TEST_WORDS
from backend.app.main import app
from backend.app.modules.database.base import Base
from backend.app.modules.database.models import Folder, Reader
from backend.app.modules.database.recording import record_session, session_difficulty
from backend.app.modules.database.session import get_db
from backend.app.modules.reading_engine.ai_bridge import AiOutcome
from backend.app.modules.reading_speed.models import (
    DifficultyLevel,
    DifficultyMetrics,
    SessionAnalytics,
)
from backend.app.modules.reading_speed.service import reading_speed_service

READER = "live-reader"


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Seed the live-reader row so foreign keys and auth work.
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Reader(id=READER, device_prefs={}))
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def client(db_factory):
    _reader_cache = {}

    def override():
        session = db_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    def override_auth():
        """Return the test reader without requiring a cookie."""
        if "reader" not in _reader_cache:
            with db_factory() as db:
                _reader_cache["reader"] = db.get(Reader, READER)
        return _reader_cache["reader"]

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_current_reader] = override_auth
    # The baseline store is process-wide and deliberately outlives requests, so a
    # test that calibrates would otherwise leak its baseline into the next one.
    reading_speed_service._baselines.pop(READER, None)
    yield TestClient(app)
    app.dependency_overrides.clear()
    reading_speed_service._baselines.pop(READER, None)


def analytics_with(*levels: DifficultyLevel, **overrides) -> SessionAnalytics:
    """A finished-session summary whose pages carry the given verdicts.

    Every field is overridable — the defaults are merged rather than passed
    alongside `**overrides`, so a test that needs an unmeasurable pace can say
    `session_wpm=0.0` instead of building the whole model by hand.
    """

    defaults = {
        "session_id": "test-session",
        "reader_id": READER,
        "baseline_wpm": 200.0,
        "session_wpm": 190.0,
        "words_read": 1200,
        "pages_read": len(levels),
        "reading_duration_ms": 380_000,
        "wall_duration_ms": 420_000,
    }

    return SessionAnalytics(
        pages=tuple(
            DifficultyMetrics(page_index=index, words=300, difficulty=level)
            for index, level in enumerate(levels)
        ),
        **{**defaults, **overrides},
    )


def store(db_factory, analytics: SessionAnalytics, **kwargs):
    with db_factory() as db:
        row = record_session(db, analytics=analytics, **kwargs)
        db.commit()
        return row.id


# ------------------------------------------------------------- session difficulty


def test_a_session_of_unrated_pages_stays_unrated():
    """UNKNOWN is not a middle rating, and averaging must not turn it into one."""

    assert session_difficulty(analytics_with()) is DifficultyLevel.UNKNOWN
    assert (
        session_difficulty(analytics_with(DifficultyLevel.UNKNOWN, DifficultyLevel.UNKNOWN))
        is DifficultyLevel.UNKNOWN
    )


def test_the_majority_page_verdict_wins_and_ties_go_to_the_harder():
    """A session split evenly between hard and easy pages was not an easy read."""

    assert (
        session_difficulty(
            analytics_with(
                DifficultyLevel.LOW,
                DifficultyLevel.LOW,
                DifficultyLevel.HIGH,
                DifficultyLevel.UNKNOWN,
            )
        )
        is DifficultyLevel.LOW
    )
    assert (
        session_difficulty(analytics_with(DifficultyLevel.LOW, DifficultyLevel.HIGH))
        is DifficultyLevel.HIGH
    )


def test_an_unrated_session_serialises_as_no_difficulty(client, db_factory):
    """The website gets `null`, not "Medium". This is the whole point of UNKNOWN."""

    session_id = store(db_factory, analytics_with(DifficultyLevel.UNKNOWN))

    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["difficulty"] is None


def test_page_verdicts_reach_the_website_in_its_own_vocabulary(client, db_factory):
    session_id = store(db_factory, analytics_with(DifficultyLevel.HIGH, DifficultyLevel.HIGH))

    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["difficulty"] == "Hard"
    assert body["pagesRead"] == 2
    # The reading clock, not the wall clock: 380s of reading is six minutes, even
    # though seven minutes passed.
    assert body["readingTimeMin"] == 6


def test_reading_progress_reaches_the_website_unchanged(client, db_factory):
    """The numbers on the Session Details page are the ones analytics measured.

    Reading Speed's whole chain ends here, so this is where a value silently
    dropped, rounded away or replaced by a narration figure would show up. It is
    the one check that spans analytics, the row and the HTTP edge.
    """

    analytics = analytics_with(DifficultyLevel.LOW, DifficultyLevel.LOW)
    session_id = store(db_factory, analytics)

    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["wordsRead"] == analytics.words_read == 1200
    assert body["wpm"] == round(analytics.session_wpm) == 190
    assert body["pagesRead"] == analytics.pages_read


# --------------------------------------------------------------------- AI summary


def test_a_session_with_no_lookups_has_no_summary(client, db_factory):
    """`AiBridge.review()` refuses to summarise nothing. That is not an error."""

    session_id = store(db_factory, analytics_with(DifficultyLevel.LOW), review=None)

    response = client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json()["summary"] is None


def test_a_failed_ai_review_is_stored_as_no_summary(client, db_factory):
    """An unreachable Groq is a failed outcome, not an exception — and not text."""

    failed = AiOutcome(
        capability="summary_generator", ok=False, error="No lookups recorded this session"
    )
    session_id = store(db_factory, analytics_with(DifficultyLevel.LOW), review=failed)

    assert client.get(f"/api/sessions/{session_id}").json()["summary"] is None


def test_a_successful_review_is_stored_whole(client, db_factory):
    """The summary reaches the page; the rest is kept for the deferred pages."""

    review = AiOutcome(
        capability="summary_generator",
        ok=True,
        data={
            "session_summary": "The keeper climbed the stairs.",
            "quiz": [{"question": "Who climbed?", "options": ["A", "B"], "correct_answer": "A"}],
            "flashcards": [{"word": "keeper", "fun_definition": "one who keeps"}],
        },
    )
    with db_factory() as db:
        row = record_session(db, analytics=analytics_with(DifficultyLevel.LOW), review=review)
        db.commit()
        session_id, payload = row.id, row.review_payload

    assert payload["quiz"][0]["correct_answer"] == "A"
    assert client.get(f"/api/sessions/{session_id}").json()["summary"] == (
        "The keeper climbed the stairs."
    )


# ----------------------------------------------------------------- reading test


def test_an_implausible_timing_is_refused_and_leaves_the_baseline_alone(client):
    """A stopwatch left running must not become a permanent wrong baseline."""

    before = client.get("/api/me").json()

    response = client.post("/api/reading-test", json={"elapsedMs": 400})
    assert response.status_code == 422
    assert "ms of reading" in response.json()["detail"]

    after = client.get("/api/me").json()
    assert after["wpm"] == before["wpm"]
    assert after["wpmIsMeasured"] is False


def test_a_plausible_timing_measures_the_baseline(client):
    """The server counts the words. 65 words in 20s is a shade under 200 wpm."""

    expected = round(READING_TEST_WORDS / (20_000 / 60_000))

    body = client.post("/api/reading-test", json={"elapsedMs": 20_000}).json()
    assert body["wpm"] == expected
    assert body["wpmIsMeasured"] is True
    assert client.get("/api/me").json()["wpm"] == expected


def test_an_unknown_preset_is_refused(client):
    assert client.post("/api/reading-speed/preset", json={"preset": "9x"}).status_code == 422


def test_a_preset_is_a_claim_not_a_measurement(client):
    """`is_evidence` stays False, because analytics treats the two differently."""

    body = client.post("/api/reading-speed/preset", json={"preset": "1.5x"}).json()
    assert body == {"wpm": 260, "wpmIsMeasured": False}


# --------------------------------------------------------------------- folders


def test_deleting_a_folder_unfiles_its_sessions(client, db_factory):
    """The sessions survive. SQLite does not enforce the FK, so the route does."""

    with db_factory() as db:
        folder = Folder(name="Biology")
        db.add(folder)
        db.flush()
        folder_id = folder.id
        row = record_session(db, analytics=analytics_with(DifficultyLevel.LOW))
        row.folder_id = folder_id
        db.commit()
        session_id = row.id

    assert client.delete(f"/api/folders/{folder_id}").status_code == 204

    listing = client.get("/api/sessions").json()
    assert listing["folders"] == []
    assert [s["id"] for s in listing["sessions"]] == [session_id]
    assert listing["sessions"][0]["folderId"] is None


def test_moving_a_session_into_a_missing_folder_is_a_404(client, db_factory):
    session_id = store(db_factory, analytics_with(DifficultyLevel.LOW))

    response = client.patch(f"/api/sessions/{session_id}", json={"folderId": "no-such-folder"})
    assert response.status_code == 404


def test_a_session_can_be_moved_out_of_every_folder(client, db_factory):
    """`folderId: null` is a value, not an omission."""

    session_id = store(db_factory, analytics_with(DifficultyLevel.LOW))
    with db_factory() as db:
        folder = Folder(name="English")
        db.add(folder)
        db.commit()
        folder_id = folder.id

    client.patch(f"/api/sessions/{session_id}", json={"folderId": folder_id})
    assert client.get("/api/sessions").json()["sessions"][0]["folderId"] == folder_id

    client.patch(f"/api/sessions/{session_id}", json={"folderId": None})
    assert client.get("/api/sessions").json()["sessions"][0]["folderId"] is None


# --------------------------------------------------------------------- analysis


def store_on(db_factory, days_ago: int, analytics: SessionAnalytics, **kwargs) -> None:
    """Store a session dated `days_ago` calendar days back.

    At local noon, so the row cannot drift into the neighbouring day: an evening
    session and a machine east of Greenwich is exactly how a UTC/local mix-up
    hides, and this helper must not be the thing that hides it. Converted to naive
    UTC on the way in, which is what SQLite holds — see `models.as_utc`.
    """

    with db_factory() as db:
        row = record_session(db, analytics=analytics, **kwargs)
        noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        # Subtracting from an aware value keeps today's offset, so a day across a
        # clock change lands at 11:00 or 13:00 — still comfortably inside the day.
        row.created_at = (noon - timedelta(days=days_ago)).astimezone(timezone.utc).replace(
            tzinfo=None
        )
        db.commit()


def analysis(client, range_key: str) -> dict:
    response = client.get(f"/api/analysis?range={range_key}")
    assert response.status_code == 200
    return response.json()


def test_each_time_filter_selects_its_own_window(client, db_factory):
    """The four filters must actually differ, which needs four different windows.

    Today is one calendar day, not the last 24 hours — so yesterday's reading is
    out of "Today" however few hours ago it was.
    """

    for days_ago in (0, 3, 20, 200):
        store_on(db_factory, days_ago, analytics_with(DifficultyLevel.LOW))

    assert len(analysis(client, "today")["pagesPerDay"]) == 1
    assert len(analysis(client, "week")["pagesPerDay"]) == 2
    assert len(analysis(client, "month")["pagesPerDay"]) == 3
    assert len(analysis(client, "all")["pagesPerDay"]) == 4


def test_the_oldest_day_comes_first(client, db_factory):
    """The x-axis reads left to right, and the backend owns that order."""

    for days_ago in (1, 4, 0):
        store_on(db_factory, days_ago, analytics_with(DifficultyLevel.LOW))

    days = [point["dayStartMs"] for point in analysis(client, "week")["readingTimePerDay"]]
    assert days == sorted(days)


def test_an_empty_database_is_empty_rather_than_an_error(client):
    body = analysis(client, "all")
    assert body["empty"] is True
    assert body["pagesPerDay"] == []
    assert body["speedTrend"] == []


def test_a_range_with_no_reading_in_it_is_empty_even_though_the_database_is_not(
    client, db_factory
):
    """`empty` is a statement about the window, not about the reader's history.

    A reader who last read in March has an empty "Today" and a full "All Time",
    and the page's empty state has to be reachable from a database with rows in
    it — otherwise nobody ever sees it after the first session.
    """

    store_on(db_factory, 200, analytics_with(DifficultyLevel.LOW))

    assert analysis(client, "today")["empty"] is True
    assert analysis(client, "all")["empty"] is False


def test_two_sittings_in_one_day_are_one_point(client, db_factory):
    """Counters add; rates average. A day does not have a total words-per-minute."""

    store_on(
        db_factory,
        1,
        analytics_with(
            DifficultyLevel.LOW,
            session_wpm=150.0,
            words_read=300,
            lookup_count=2,
            reading_duration_ms=120_000,
        ),
    )
    store_on(
        db_factory,
        1,
        analytics_with(
            DifficultyLevel.LOW,
            DifficultyLevel.LOW,
            session_wpm=190.0,
            words_read=600,
            lookup_count=3,
            reading_duration_ms=180_000,
        ),
    )

    body = analysis(client, "week")
    assert len(body["pagesPerDay"]) == 1
    assert body["pagesPerDay"][0]["pages"] == 3
    assert body["lookupsPerDay"][0]["lookups"] == 5
    assert body["readingTimePerDay"][0]["minutes"] == 5
    assert body["speedTrend"][0]["wpm"] == 170


def test_an_unmeasurable_session_is_left_out_of_the_daily_pace(client, db_factory):
    """`session_wpm == 0.0` means "not measurable", and it is not a slow day.

    Averaging it in halves a real 200 wpm day because one page was flicked past
    too fast to time. The page count still counts — the reading happened, only
    its pace is unknown.
    """

    store_on(db_factory, 1, analytics_with(DifficultyLevel.LOW, session_wpm=0.0))
    store_on(db_factory, 1, analytics_with(DifficultyLevel.LOW, session_wpm=200.0))

    body = analysis(client, "week")
    assert body["speedTrend"][0]["wpm"] == 200
    assert body["pagesPerDay"][0]["pages"] == 2


def test_a_day_with_nothing_measurable_reports_no_pace_rather_than_zero(client, db_factory):
    """A gap in the line is true. A zero is a claim that the reader read at 0 wpm."""

    store_on(db_factory, 1, analytics_with(DifficultyLevel.LOW, session_wpm=0.0))

    body = analysis(client, "week")
    assert body["speedTrend"][0]["wpm"] is None
    assert body["pagesPerDay"][0]["pages"] == 1


def test_an_unrated_session_is_left_out_of_the_daily_difficulty(client, db_factory):
    """UNKNOWN is not a fourth point on the Easy/Medium/Hard axis."""

    store_on(db_factory, 1, analytics_with(DifficultyLevel.UNKNOWN))
    store_on(db_factory, 1, analytics_with(DifficultyLevel.HIGH))

    point = analysis(client, "week")["difficultyTrend"][0]
    assert point["difficulty"] == 3
    assert point["difficultyLabel"] == "Hard"


def test_a_day_of_unrated_sessions_has_no_difficulty_at_all(client, db_factory):
    store_on(db_factory, 1, analytics_with(DifficultyLevel.UNKNOWN))

    point = analysis(client, "week")["difficultyTrend"][0]
    assert point["difficulty"] is None
    assert point["difficultyLabel"] is None


def test_a_difficulty_tie_reports_the_harder_day(client, db_factory):
    """One Medium and one Hard is 2.5, and a day is not easier than its hardest read.

    `round()` would banker's-round that to Medium. Every other tie in TaleTrace
    breaks toward the harder read, and this one has to as well.
    """

    store_on(db_factory, 1, analytics_with(DifficultyLevel.MEDIUM))
    store_on(db_factory, 1, analytics_with(DifficultyLevel.HIGH))

    assert analysis(client, "week")["difficultyTrend"][0]["difficultyLabel"] == "Hard"


def test_a_very_short_session_still_appears(client, db_factory):
    """No word-count threshold, anywhere.

    A page read at breakfast is a legitimate short session, and dropping small
    rows to tidy up a chart would drop it. Bad data is removed by provenance —
    see `scripts/seed_history.py` — never by size.
    """

    store_on(
        db_factory,
        1,
        analytics_with(DifficultyLevel.LOW, words_read=1, reading_duration_ms=2_000),
    )

    body = analysis(client, "week")
    assert body["empty"] is False
    assert body["pagesPerDay"][0]["pages"] == 1
    # Two seconds of reading rounds to nought minutes, and the point is still there.
    assert body["readingTimePerDay"][0]["minutes"] == 0


def test_the_day_a_session_belongs_to_is_the_readers_day(client, db_factory):
    """`dayStartMs` is local midnight, so the browser's label matches the bucket."""

    store_on(db_factory, 2, analytics_with(DifficultyLevel.LOW))

    day_start_ms = analysis(client, "week")["pagesPerDay"][0]["dayStartMs"]
    expected = datetime.now().astimezone().date() - timedelta(days=2)
    stamped = datetime.fromtimestamp(day_start_ms / 1000).astimezone()
    assert stamped.date() == expected
    assert (stamped.hour, stamped.minute) == (0, 0)


def test_an_unknown_time_filter_is_refused(client):
    """A typo is a 422, not a silent week — the four filters are a closed set."""

    assert client.get("/api/analysis?range=fortnight").status_code == 422


# ---------------------------------------------------------- quizzes & flashcards
#
# The quiz and flashcard routes are reads: they merge `review_payload` blobs that
# the AI Engine wrote when each session ended. No route in this section may call
# the AI Engine, and no test here invokes Groq — the payloads are handcrafted
# literals, the smallest structures that exercise every code path in `review.py`.


def _review_with_quiz_and_cards(**overrides):
    """An AiOutcome whose payload has one scoreable question and one flashcard."""

    payload = {
        "session_summary": "A test session.",
        "quiz": [
            {
                "question": "What is photosynthesis?",
                "options": [
                    "Energy from light",
                    "Energy from heat",
                    "Energy from sound",
                    "Energy from wind",
                ],
                "correct_answer": "Energy from light",
            }
        ],
        "flashcards": [
            {"word": "chlorophyll", "fun_definition": "The green pigment in leaves."}
        ],
        "words_learned": [
            {"word": "chlorophyll", "takeaway": "Makes leaves green."}
        ],
    }
    payload.update(overrides)
    return AiOutcome(capability="summary_generator", ok=True, data=payload)


def store_with_review(db_factory, review, **kwargs):
    """Store a session with an AiOutcome and return its id."""

    return store(db_factory, analytics_with(DifficultyLevel.LOW), review=review, **kwargs)


# ----------------------------------------------------------------- quiz generation


def test_a_session_with_review_yields_a_quiz(client, db_factory):
    """The happy path: one session, one question, a quizId and no answer key."""

    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    response = client.post("/api/quiz", json={"sessionIds": [sid]})
    assert response.status_code == 200

    body = response.json()
    assert "quizId" in body
    assert len(body["questions"]) == 1

    question = body["questions"][0]
    assert question["question"] == "What is photosynthesis?"
    assert len(question["options"]) == 4
    # The answer key must never reach the browser.
    assert "correct_answer" not in question
    assert "correct_index" not in question
    assert "correctIndex" not in question


def test_quiz_merges_questions_from_several_sessions(client, db_factory):
    """Round-robin: each session contributes before any one dominates."""

    review_a = _review_with_quiz_and_cards(
        quiz=[
            {
                "question": "What is a cell?",
                "options": ["A unit of life", "A battery", "A room"],
                "correct_answer": "A unit of life",
            },
            {
                "question": "What is mitosis?",
                "options": ["Cell division", "Cell death"],
                "correct_answer": "Cell division",
            },
        ]
    )
    review_b = _review_with_quiz_and_cards(
        quiz=[
            {
                "question": "What is DNA?",
                "options": ["Genetic material", "A protein", "A lipid"],
                "correct_answer": "Genetic material",
            }
        ]
    )

    sid_a = store_with_review(db_factory, review_a)
    sid_b = store_with_review(db_factory, review_b)

    body = client.post("/api/quiz", json={"sessionIds": [sid_a, sid_b]}).json()
    questions = [q["question"] for q in body["questions"]]

    # All three questions are present, round-robin interleaved. The exact order
    # depends on which UUID sorts first (the tie-breaker when created_at is the
    # same), so we assert the full set rather than one particular interleaving.
    assert set(questions) == {"What is a cell?", "What is DNA?", "What is mitosis?"}
    assert len(questions) == 3


def test_quiz_is_capped_at_ten_questions(client, db_factory):
    """A month of reading must not produce a hundred-question quiz."""

    review = _review_with_quiz_and_cards(
        quiz=[
            {
                "question": f"Question {i}?",
                "options": ["A", "B", "C"],
                "correct_answer": "A",
            }
            for i in range(15)
        ]
    )
    sid = store_with_review(db_factory, review)

    body = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    assert len(body["questions"]) == 10


def test_duplicate_questions_are_merged_across_sessions(client, db_factory):
    """The same question from two sessions appears once, not twice."""

    same_quiz = [
        {
            "question": "What is photosynthesis?",
            "options": ["Light energy", "Heat energy"],
            "correct_answer": "Light energy",
        }
    ]
    sid_a = store_with_review(db_factory, _review_with_quiz_and_cards(quiz=same_quiz))
    sid_b = store_with_review(db_factory, _review_with_quiz_and_cards(quiz=same_quiz))

    body = client.post("/api/quiz", json={"sessionIds": [sid_a, sid_b]}).json()
    assert len(body["questions"]) == 1


def test_quiz_for_sessions_without_review_is_a_404(client, db_factory):
    """A session read straight through has no quiz. The page shows NO_REVIEW."""

    sid = store(db_factory, analytics_with(DifficultyLevel.LOW), review=None)

    response = client.post("/api/quiz", json={"sessionIds": [sid]})
    assert response.status_code == 404
    assert "nothing to build" in response.json()["detail"].lower()


def test_quiz_with_empty_selection_is_a_422(client):
    """Select at least one session first."""

    response = client.post("/api/quiz", json={"sessionIds": []})
    assert response.status_code == 422


def test_quiz_for_a_missing_session_is_a_404(client):
    """A stale list is a 404, not a quietly shorter quiz."""

    response = client.post("/api/quiz", json={"sessionIds": ["no-such-session"]})
    assert response.status_code == 404


# -------------------------------------------------------------- quiz submission


def test_quiz_submission_is_scored_server_side(client, db_factory):
    """The server rebuilds the answer key. No answers travel to the browser."""

    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    quiz = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    quiz_id = quiz["quizId"]
    question_id = quiz["questions"][0]["id"]

    # Submit the correct answer (index 0 = "Energy from light").
    result = client.post(
        "/api/quiz/submit",
        json={"quizId": quiz_id, "answers": {question_id: 0}},
    ).json()

    assert result["score"] == 1
    assert result["total"] == 1
    assert result["percent"] == 100
    assert len(result["results"]) == 1
    assert result["results"][0]["correct"] is True
    assert result["results"][0]["correctIndex"] == 0


def test_a_wrong_answer_scores_zero(client, db_factory):
    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    quiz = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    quiz_id = quiz["quizId"]
    question_id = quiz["questions"][0]["id"]

    result = client.post(
        "/api/quiz/submit",
        json={"quizId": quiz_id, "answers": {question_id: 2}},
    ).json()

    assert result["score"] == 0
    assert result["percent"] == 0
    assert result["results"][0]["correct"] is False


def test_an_unanswered_question_is_wrong(client, db_factory):
    """Skipping a question is wrong, same as on paper."""

    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    quiz = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    # Submit with no answers at all.
    result = client.post(
        "/api/quiz/submit",
        json={"quizId": quiz["quizId"], "answers": {}},
    ).json()

    assert result["score"] == 0
    assert result["total"] == 1


def test_submitting_after_session_deletion_is_a_404(client, db_factory):
    """A session deleted mid-quiz means the quiz can no longer be scored."""

    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    quiz = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    client.delete(f"/api/sessions/{sid}")

    response = client.post(
        "/api/quiz/submit",
        json={"quizId": quiz["quizId"], "answers": {}},
    )
    assert response.status_code == 404
    assert "no longer be scored" in response.json()["detail"]


# ------------------------------------------------------------------- flashcards


def test_a_session_with_review_yields_flashcards(client, db_factory):
    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    body = client.post("/api/flashcards", json={"sessionIds": [sid]}).json()
    assert len(body["cards"]) >= 1

    card = body["cards"][0]
    assert card["term"] == "chlorophyll"
    assert "green" in card["definition"].lower()
    assert "id" in card


def test_flashcards_deduplicate_across_sessions(client, db_factory):
    """The same word from two sessions appears as one card, not two."""

    sid_a = store_with_review(db_factory, _review_with_quiz_and_cards())
    sid_b = store_with_review(db_factory, _review_with_quiz_and_cards())

    body = client.post("/api/flashcards", json={"sessionIds": [sid_a, sid_b]}).json()

    terms = [card["term"].lower() for card in body["cards"]]
    assert terms.count("chlorophyll") == 1


def test_flashcards_for_sessions_without_review_is_a_404(client, db_factory):
    sid = store(db_factory, analytics_with(DifficultyLevel.LOW), review=None)

    response = client.post("/api/flashcards", json={"sessionIds": [sid]})
    assert response.status_code == 404


def test_flashcards_with_empty_selection_is_a_422(client):
    response = client.post("/api/flashcards", json={"sessionIds": []})
    assert response.status_code == 422


def test_quiz_id_is_deterministic_across_requests(client, db_factory):
    """The same sessions produce the same quizId, so submit can rebuild the key."""

    sid = store_with_review(db_factory, _review_with_quiz_and_cards())

    quiz_1 = client.post("/api/quiz", json={"sessionIds": [sid]}).json()
    quiz_2 = client.post("/api/quiz", json={"sessionIds": [sid]}).json()

    assert quiz_1["quizId"] == quiz_2["quizId"]
    assert quiz_1["questions"] == quiz_2["questions"]


def test_quiz_id_is_order_independent(client, db_factory):
    """Selecting the same three sessions in a different order is the same quiz."""

    sid_a = store_with_review(db_factory, _review_with_quiz_and_cards())
    sid_b = store_with_review(
        db_factory,
        _review_with_quiz_and_cards(
            quiz=[
                {
                    "question": "What is DNA?",
                    "options": ["Genetic material", "A protein"],
                    "correct_answer": "Genetic material",
                }
            ]
        ),
    )

    quiz_ab = client.post("/api/quiz", json={"sessionIds": [sid_a, sid_b]}).json()
    quiz_ba = client.post("/api/quiz", json={"sessionIds": [sid_b, sid_a]}).json()

    assert quiz_ab["quizId"] == quiz_ba["quizId"]
