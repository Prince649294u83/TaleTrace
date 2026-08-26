"""The website's API. One router, read-mostly, no arithmetic of its own.

TaleTrace's website is a companion, not a reader: there is no "start session"
button anywhere in it, because reading happens on the ESP32 rig. So this surface
is almost entirely *reads* — it shows what the rig already produced — plus the
handful of writes that belong to a person sitting at a browser rather than
holding a book: name a session, file it in a folder, take the reading-speed test,
change a preference.

Every number here comes from a module that measured it. `session_wpm` is the
figure `ReadingSpeedService` computed, `difficulty` is the verdict
`analytics.assess_page` reached, `deviceStatus` is what `live_session.preflight()`
found on the network a moment ago. If a figure is wrong on screen it is wrong
upstream, and there is nowhere in this file it could have been introduced.

Bare JSON, not `ResponseEnvelope`
---------------------------------
The module routers wrap every response in `{success, message, data, errors}` for
the device-facing API, where a caller with no browser needs the failure in the
body. The website has a browser: it has HTTP status codes and an `ErrorState`
component that renders `error.message`. Wrapping would mean every page unwrapping
a level that carries no information the status line didn't already.

camelCase, not snake_case
-------------------------
The field names match what the React pages already destructure, so no page or
component changes to read a real backend. That was the frontend's stated contract
and it is cheaper to honour here than to renegotiate across eleven files.

Not authenticated
-----------------
There is no login on this surface and no reader id in any path. The server holds
exactly one reader row, the website's login screen is a `localStorage` gate, and
no password ever reaches this process. That is fine bound to localhost and is not
fine anywhere else — the reader id becomes a session cookie when it stops being.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from backend.app.api.auth import get_current_reader
from backend.app.live_session import preflight
from backend.app.modules.database import review
from backend.app.modules.database.analysis import DIFFICULTY_SCORES, RANGE_DAYS, daily_history
from backend.app.modules.database.models import Folder, Reader
from backend.app.modules.database.models import Session as SessionRow
from backend.app.modules.database.models import as_utc
from backend.app.modules.database.recording import store_baseline
from backend.app.modules.database.session import get_db
from backend.app.modules.reading_speed.calibration import CalibrationError
from backend.app.modules.reading_speed.models import DifficultyLevel
from backend.app.modules.reading_speed.service import reading_speed_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["companion"])


# The reading-speed test passage, and the only copy of it. The client renders
# what this endpoint returns and never counts the words, because a client that
# counts its own words can report any word count it likes and calibration would
# believe it.
#
# 68 words is above `MIN_CALIBRATION_WORDS` (50) but not by much, and a short
# passage measures a slow reader better than a fast one — 68 words at 400 wpm is
# ten seconds, which is exactly `MIN_CALIBRATION_MS`. A longer passage would
# measure better; this one is the text the design was drawn around.
# ponytail: swap in a 200-word passage if fast readers hit the 10s floor.
READING_TEST_PASSAGE = (
    "The lighthouse keeper climbed the spiral stairs before dawn, counting each "
    "step out of habit rather than need. Fog had rolled in overnight, thick enough "
    "to swallow the shoreline whole, and the beam above would matter more than "
    "usual. He had done this for eleven years, yet the climb never felt routine — "
    "every morning carried its own small weather, its own reason to pay attention."
)

# The three presets the onboarding screen offers, and what each claims in words
# per minute. Same numbers the frontend mock used, so a reader who picked "1.5x"
# before the backend existed still gets 260.
SPEED_PRESETS: dict[str, float] = {"1x": 200.0, "1.5x": 260.0, "2x": 320.0}

# The backend's four difficulty levels, in the website's three words.
#
# UNKNOWN maps to `None`, never to "Medium". UNKNOWN is not a middle rating — it
# is `assess_page` declining to rate: a page read slowly with no lookups and no
# re-reads is an interruption, not a hard page. Rendering that as "Medium" would
# invent a measurement, and the whole reason the level exists is to avoid that.
DIFFICULTY_LABELS: dict[DifficultyLevel, str | None] = {
    DifficultyLevel.LOW: "Easy",
    DifficultyLevel.MEDIUM: "Medium",
    DifficultyLevel.HIGH: "Hard",
    DifficultyLevel.UNKNOWN: None,
}


def _word_count(text: str) -> int:
    """Words in a passage, ignoring stray punctuation.

    A bare em dash between two clauses is not a word, and counting it inflates
    every measured baseline by a word and a half per hundred.
    """

    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


READING_TEST_WORDS = _word_count(READING_TEST_PASSAGE)


# ------------------------------------------------------------------------ schemas


class ReaderPatch(BaseModel):
    """A partial update. Only the fields actually sent are applied.

    `model_fields_set` is what distinguishes "theme omitted" from "theme set to
    null", which matters because the Settings page sends one field at a time.
    """

    readerType: str | None = None
    devicePrefs: dict[str, Any] | None = None
    theme: str | None = None
    name: str | None = None
    email: str | None = None
    profileCompleted: bool | None = None


class ReadingTestSubmission(BaseModel):
    """How long the reader took over the passage.

    No word count: the server knows it. `gt=0` rejects the obviously impossible;
    everything else — a timer left running, a passage skipped — is
    `calibrate_from_passage`'s judgement, and it raises rather than clamping.
    """

    elapsedMs: int = Field(gt=0)


class PresetSubmission(BaseModel):
    preset: str


class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SessionPatch(BaseModel):
    """Rename, or move to a folder. `folderId: null` means "out of every folder"."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    folderId: str | None = None


class SessionSelection(BaseModel):
    """Which sessions to build a quiz or a deck from.

    Not `min_length=1`: an empty selection is refused in the route instead, so the
    reader reads "Select at least one session" rather than Pydantic's account of
    which field failed which constraint.
    """

    sessionIds: list[str] = Field(default_factory=list)


class QuizSubmission(BaseModel):
    """Answers to a quiz, as `{questionId: chosen option index}`."""

    quizId: str
    answers: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------- serialisation


def _epoch_ms(moment: datetime) -> int:
    """A stored timestamp as epoch milliseconds, whatever SQLite handed back."""

    return int(as_utc(moment).timestamp() * 1000)


def _difficulty(row: SessionRow) -> str | None:
    """The stored level in the website's vocabulary, or `None` if unrated."""

    try:
        level = DifficultyLevel(row.difficulty)
    except ValueError:  # a level written by an older build
        return None
    return DIFFICULTY_LABELS[level]


def _session_summary_row(row: SessionRow) -> dict[str, Any]:
    """One entry in the sessions list. Deliberately thin — the list shows names.

    `createdAt` goes out as epoch milliseconds, matching what the frontend already
    sorts and formats. The date *string* is formatted in the browser, not here:
    the browser knows the reader's timezone and locale and this process only knows
    the server's, which is the same machine today and will not be forever.
    """

    return {
        "id": row.id,
        "name": row.name,
        "folderId": row.folder_id,
        "createdAt": _epoch_ms(row.created_at),
    }


def _session_detail(row: SessionRow) -> dict[str, Any]:
    """Everything the Session Details page shows.

    `readingTimeMin` rounds the reading clock, which excludes Meaning Mode holds
    and idle time — so it is smaller than the wall clock, on purpose. A reader who
    spent forty minutes with a book and twelve of them looking up words read for
    twenty-eight.

    `wordsRead` is reading progress and `wpm` is that progress over that clock.
    Neither is a narration figure: words the TTS engine spoke are counted
    separately by the Audio Engine and are not served here, because a reader who
    listened to a page while pointing at four words read a page.
    """

    return {
        **_session_summary_row(row),
        "readingTimeMin": round(row.reading_duration_ms / 60_000),
        "wordsRead": row.words_read,
        "pagesRead": row.pages_read,
        "lookups": row.lookup_count,
        "wpm": round(row.session_wpm),
        "difficulty": _difficulty(row),
        "summary": row.summary,
    }


# ------------------------------------------------------------------------- routes


@router.get("/me")
def get_me(reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """The reading profile."""

    baseline = reading_speed_service.baseline_for(reader.id)
    return {
        "id": reader.id,
        "name": reader.name,
        "email": reader.email,
        "profileCompleted": bool(reader.profile_completed),
        "wpm": round(baseline.baseline_wpm),
        "wpmIsMeasured": baseline.is_evidence,
        "readingSpeedPreset": reader.reading_speed_preset,
        "readerType": reader.reader_type,
        "devicePrefs": reader.device_prefs or {},
        "theme": reader.theme,
    }


@router.patch("/me")
def patch_me(patch: ReaderPatch, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Update whichever preferences were sent.

    `wpm` is not settable here. A baseline is either measured from the passage or
    claimed via a preset, and both of those routes record *how* it was arrived at.
    A bare number assigned through the settings screen would lose that, and
    analytics treats a measured baseline and a claimed one differently.
    """

    sent = patch.model_fields_set

    if "readerType" in sent:
        reader.reader_type = patch.readerType
        reader.profile_completed = 1  # Replicates the original mock backend behaviour during setup
    if "theme" in sent and patch.theme:
        reader.theme = patch.theme
    if "devicePrefs" in sent and patch.devicePrefs is not None:
        # Merged, not replaced: the Settings page sends one toggle at a time, and
        # replacing would silently reset the other three.
        reader.device_prefs = {**(reader.device_prefs or {}), **patch.devicePrefs}
    if "name" in sent:
        reader.name = patch.name
    if "email" in sent:
        reader.email = patch.email
    if "profileCompleted" in sent:
        reader.profile_completed = 1 if patch.profileCompleted else 0

    db.commit()

    return get_me(reader, db)


@router.get("/device/status")
def device_status() -> dict[str, Any]:
    """Whether the rig is reachable right now, and what else is ready.

    `preflight()` verbatim, because the answer to "is the device connected" must
    be the same one `live_session --check` gives. Two code paths probing the same
    hardware is how a dashboard ends up saying Connected while a session refuses
    to start.

    The badge follows the camera alone. The camera is the one component with no
    fallback — a session without it has no text at all — while missing buttons
    degrade to a scheduled rehearsal and a missing Groq key degrades to rougher
    text. Those belong in the detail rows, not in the headline.

    ponytail: synchronous HTTP probes with 1.5s + 0.3s timeouts, so a dashboard
    load waits on the network. Fine for one reader at one desk; cache the last
    result with a short TTL if this is ever polled.
    """

    rows = preflight()
    checks = [{"name": name, "ok": ok, "detail": detail} for name, ok, detail in rows]
    camera = next((c for c in checks if c["name"] == "ESP32-CAM"), None)
    return {
        "deviceStatus": "connected" if camera and camera["ok"] else "disconnected",
        "checks": checks,
    }


@router.get("/dashboard")
def dashboard(reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Today's reading, plus the rig's status.

    "Today" is the server's local calendar day, matching the frontend mock's
    `toDateString()` comparison. Same machine, same day; when the two are ever
    apart, this is the line that has to learn about the reader's timezone.
    """

    baseline = reading_speed_service.baseline_for(reader.id)
    total = db.scalar(select(SessionRow.id).where(SessionRow.reader_id == reader.id).limit(1))

    # Compared naive-UTC, because that is what SQLite stores — see `_epoch_ms`.
    # The day boundary is the *reader's* midnight converted to UTC, not UTC
    # midnight, or "today's pages" would reset in the middle of an evening.
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    todays = list(db.scalars(select(SessionRow).where(SessionRow.reader_id == reader.id, SessionRow.created_at >= since)))

    return {
        "empty": total is None,
        "wpm": round(baseline.baseline_wpm),
        "pagesToday": sum(row.pages_read for row in todays),
        "lookupsToday": sum(row.lookup_count for row in todays),
        **device_status(),
    }


AnalysisRange = Literal["today", "week", "month", "all"]
assert set(AnalysisRange.__args__) == set(RANGE_DAYS), "range filters have drifted apart"


@router.get("/analysis")
def analysis(
    range_: AnalysisRange = Query("week", alias="range"),
    reader: Reader = Depends(get_current_reader),
    db: OrmSession = Depends(get_db),
) -> dict[str, Any]:
    """The Analysis page's five charts, one point per day of real reading.

    `range` is a `Literal`, so an unrecognised filter is a 422 from FastAPI's own
    validation rather than a silent fall back to a week of data the caller did not
    ask for.

    `empty` is about the *range*, not the database: a reader with months of history
    who has not opened a book today gets the page's empty state for "Today" rather
    than five blank axes. Sessions the range excludes are not a kind of nothing.

    `wpm` and `difficulty` come out as `null` on a day whose sessions were all too
    short to measure or too quiet to rate — see `analysis.daily_history`. Recharts
    draws a gap through a null, which is the honest rendering; substituting a zero
    or a "Medium" would put a number on the chart that nothing measured.
    """

    days = daily_history(db, range_key=range_, reader_id=reader.id)
    return {
        "empty": not days,
        "readingTimePerDay": [
            {"dayStartMs": day.day_start_ms, "minutes": day.minutes} for day in days
        ],
        "pagesPerDay": [{"dayStartMs": day.day_start_ms, "pages": day.pages} for day in days],
        "lookupsPerDay": [
            {"dayStartMs": day.day_start_ms, "lookups": day.lookups} for day in days
        ],
        "speedTrend": [{"dayStartMs": day.day_start_ms, "wpm": day.wpm} for day in days],
        "difficultyTrend": [
            {
                "dayStartMs": day.day_start_ms,
                "difficulty": DIFFICULTY_SCORES[day.difficulty] if day.difficulty else None,
                "difficultyLabel": DIFFICULTY_LABELS[day.difficulty] if day.difficulty else None,
            }
            for day in days
        ],
    }


@router.get("/reading-test")
def get_reading_test() -> dict[str, Any]:
    """The passage to read, and how many words it is."""

    return {"paragraph": READING_TEST_PASSAGE, "wordCount": READING_TEST_WORDS}


@router.post("/reading-test")
def submit_reading_test(
    submission: ReadingTestSubmission, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Measure a baseline from the timed passage.

    422 rather than a clamped number when the timing is implausible, because that
    is `calibrate_from_passage`'s deliberate choice: a stopwatch left running
    yields a baseline that looks entirely reasonable and is permanently wrong, and
    every difficulty verdict for the rest of the reader's life is measured against
    it. A visible failure the reader can retry is the cheaper outcome.
    """

    try:
        baseline = reading_speed_service.calibrate(
            reader.id, word_count=READING_TEST_WORDS, elapsed_ms=submission.elapsedMs
        )
    except CalibrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    reader.reading_speed_preset = None
    store_baseline(reader, baseline)
    return {"wpm": round(baseline.baseline_wpm), "wpmIsMeasured": baseline.is_evidence}


@router.post("/reading-speed/preset")
def submit_preset(
    submission: PresetSubmission, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Record a self-reported pace, for a reader who skips the test."""

    wpm = SPEED_PRESETS.get(submission.preset)
    if wpm is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown preset '{submission.preset}'; expected one of "
            f"{', '.join(SPEED_PRESETS)}",
        )

    baseline = reading_speed_service.set_manual_baseline(reader.id, baseline_wpm=wpm)
    reader.reading_speed_preset = submission.preset
    store_baseline(reader, baseline)
    return {"wpm": round(baseline.baseline_wpm), "wpmIsMeasured": baseline.is_evidence}


# ------------------------------------------------------------- sessions & folders


@router.get("/sessions")
def list_sessions(reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Every recorded session, newest first, with the folders to file them in."""

    rows = db.scalars(select(SessionRow).where(SessionRow.reader_id == reader.id).order_by(SessionRow.created_at.desc())).all()
    folders = db.scalars(select(Folder).where(Folder.reader_id == reader.id).order_by(Folder.name)).all()
    return {
        "folders": [{"id": f.id, "name": f.name} for f in folders],
        "sessions": [_session_summary_row(row) for row in rows],
    }


def _require_session(db: OrmSession, session_id: str, reader_id: str) -> SessionRow:
    row = db.get(SessionRow, session_id)
    if row is None or row.reader_id != reader_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return row


@router.get("/sessions/{session_id}")
def session_details(session_id: str, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """One session in full.

    `summary` is `None` for a session read without Meaning Mode. That is not a
    failure: `AiBridge.review()` refuses to summarise a session with no lookups
    rather than inventing one from nothing, so the page needs an empty state and
    not an error.
    """

    return _session_detail(_require_session(db, session_id, reader.id))


@router.patch("/sessions/{session_id}")
def patch_session(
    session_id: str, patch: SessionPatch, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Rename a session, or move it into a folder (or out of all of them)."""

    row = _require_session(db, session_id, reader.id)
    sent = patch.model_fields_set

    if "name" in sent and patch.name:
        row.name = patch.name
    if "folderId" in sent:
        if patch.folderId is not None:
            folder = db.get(Folder, patch.folderId)
            if folder is None or folder.reader_id != reader.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
        row.folder_id = patch.folderId

    return _session_summary_row(row)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> Response:
    """Delete a session and everything hanging off it.

    Really deleted, not flagged. The reader asked; a session they cannot see but
    which still counts toward today's pages is worse than gone.
    """

    db.delete(_require_session(db, session_id, reader.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def create_folder(body: FolderBody, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    folder = Folder(name=body.name, reader_id=reader.id)
    db.add(folder)
    db.flush()
    return {"id": folder.id, "name": folder.name}


@router.patch("/folders/{folder_id}")
def rename_folder(
    folder_id: str, body: FolderBody, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    folder = db.get(Folder, folder_id)
    if folder is None or folder.reader_id != reader.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
    folder.name = body.name
    return {"id": folder.id, "name": folder.name}


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(folder_id: str, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> Response:
    """Delete a folder. Its sessions survive, unfiled.

    The `ON DELETE SET NULL` on `sessions.folder_id` says the same thing at the
    schema level, and it is set explicitly here because SQLite does not enforce
    foreign keys unless asked to — relying on the constraint would leave rows
    pointing at a folder that no longer exists, and the sessions list would drop
    them silently.
    """

    folder = db.get(Folder, folder_id)
    if folder is None or folder.reader_id != reader.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")

    for row in db.scalars(select(SessionRow).where(SessionRow.folder_id == folder_id, SessionRow.reader_id == reader.id)):
        row.folder_id = None
    db.delete(folder)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------- quizzes & flashcards
#
# Both of these are reads. The AI Engine wrote a quiz, a set of flashcards, the
# words learned and a summary when each session ended, and `review_payload` has
# held them ever since — so "Generate Quiz" merges rows, it does not think. No
# route in this section may call the AI Engine. A second call would spend a
# request to produce a different quiz about the same reading, and the reader would
# have no way to know why the questions changed between two clicks.


def _selected(db: OrmSession, session_ids: list[str], reader_id: str) -> list[SessionRow]:
    """The rows the reader picked, or a 4xx naming what went wrong.

    A missing id is a 404 rather than a quietly shorter quiz: the Sessions page
    offered these, so an id that no longer resolves means the list is stale, and a
    quiz built from four of the five sessions somebody selected is indistinguishable
    on screen from one built from all five.
    """

    if not session_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one session first.",
        )

    # `dict.fromkeys` de-duplicates while keeping the order the reader clicked in.
    return [_require_session(db, sid, reader_id) for sid in dict.fromkeys(session_ids)]


# Why a selection can hold no review, worded for somebody who is looking at an
# empty page and has no idea what Meaning Mode is.
NO_REVIEW = (
    "There is nothing to build from in those sessions. A quiz and flashcards are "
    "written when a session ends, from the words the reader pressed the button on "
    "— a session read straight through has none."
)


@router.post("/quiz")
def generate_quiz(
    selection: SessionSelection, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """The quiz for a selection of sessions, without the answer key.

    `questions` carries the text and the options and nothing else. The correct
    index stays in this process — see `review.quiz_id` for how submit finds it
    again without either storing it or sending it.
    """

    rows = _selected(db, selection.sessionIds, reader.id)
    questions = review.merge_quiz(rows)
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=NO_REVIEW,
        )

    return {
        "quizId": review.quiz_id(row.id for row in rows),
        "questions": [
            {"id": question.id, "question": question.question, "options": list(question.options)}
            for question in questions
        ],
    }


@router.post("/quiz/submit")
def submit_quiz(submission: QuizSubmission, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Mark a submission and return the score with the correct answers.

    The answer key is rebuilt here, from the same rows the quiz id names, rather
    than looked up: `merge_quiz` is deterministic, so re-deriving is cheaper than
    storing and cannot go stale against a restart.

    A 404 means the quiz cannot be rebuilt — a session in it was deleted while the
    reader was answering. Better than marking them against the questions that
    happen to survive, which would report a score out of a total they never saw.
    """

    session_ids = review.sessions_in(submission.quizId)
    # Ownership check: only include sessions belonging to this reader.
    rows = [
        row for row in (db.get(SessionRow, sid) for sid in session_ids)
        if row is not None and row.reader_id == reader.id
    ]
    questions = review.merge_quiz(rows) if rows else []
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That quiz can no longer be scored, because the sessions it came "
            "from are gone. Generating a new one will work.",
        )

    return review.score(questions, submission.answers)


@router.post("/flashcards")
def generate_flashcards(
    selection: SessionSelection, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """The deck for a selection of sessions, one card per distinct word.

    Not capped, deliberately — `review.merge_flashcards` says why.
    """

    rows = _selected(db, selection.sessionIds, reader.id)
    cards = review.merge_flashcards(rows)
    if not cards:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=NO_REVIEW,
        )

    return {
        "cards": [
            {"id": card.id, "term": card.term, "definition": card.definition} for card in cards
        ]
    }

