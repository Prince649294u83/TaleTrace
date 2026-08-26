"""Companion API tests — the website's contract with the reading engine.

No hardware, no network, no AI. Every test writes a session row the way
`live_session` does and then reads it back the way the website does, because the
mistakes worth catching here are all mistranslations between those two ends.

Four claims, one per mistake that would be invisible on screen:

  * an implausible reading-test timing is refused, not clamped — a stopwatch left
    running would otherwise become a permanent wrong baseline that every later
    difficulty verdict is measured against;
  * an unrated session serialises as no difficulty at all, never as "Medium" —
    UNKNOWN means the engine declined to rate, and rendering it as the middle
    rating would invent a measurement;
  * a session read without Meaning Mode has no summary and still loads — the AI
    Engine refuses to summarise nothing, which is correct, and must not read as a
    server error;
  * deleting a folder unfiles its sessions rather than taking them with it.

The database is a fresh in-memory SQLite per test, wired in by overriding the
`get_db` dependency. `StaticPool` because the default pool would hand each
connection its own private in-memory database, and a row written by one would be
invisible to the next.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.app.api.companion import READING_TEST_WORDS
from backend.app.main import app
from backend.app.modules.database.base import Base
from backend.app.modules.database.models import Folder
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
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(db_factory):
    def override():
        session = db_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    # The baseline store is process-wide and deliberately outlives requests, so a
    # test that calibrates would otherwise leak its baseline into the next one.
    reading_speed_service._baselines.pop(READER, None)
    yield TestClient(app)
    app.dependency_overrides.clear()
    reading_speed_service._baselines.pop(READER, None)


def analytics_with(*levels: DifficultyLevel, **overrides) -> SessionAnalytics:
    """A finished-session summary whose pages carry the given verdicts."""

    return SessionAnalytics(
        session_id="test-session",
        reader_id=READER,
        baseline_wpm=200.0,
        session_wpm=190.0,
        words_read=1200,
        pages_read=len(levels),
        reading_duration_ms=380_000,
        wall_duration_ms=420_000,
        pages=tuple(
            DifficultyMetrics(page_index=index, words=300, difficulty=level)
            for index, level in enumerate(levels)
        ),
        **overrides,
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
