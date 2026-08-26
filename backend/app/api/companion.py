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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from backend.app.live_session import preflight
from backend.app.modules.database.models import Folder, Reader
from backend.app.modules.database.models import Session as SessionRow
from backend.app.modules.database.recording import READER_ID, store_baseline
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


# --------------------------------------------------------------------- the reader


def _reader(db: OrmSession) -> Reader:
    """The one reader row, created on first sight.

    Created lazily rather than seeded at startup so a fresh checkout works with no
    setup step, and so the row's defaults live in exactly one place — the model.
    """

    reader = db.get(Reader, READER_ID)
    if reader is None:
        reader = Reader(id=READER_ID, device_prefs={})
        db.add(reader)
        db.flush()
    return reader


# ------------------------------------------------------------------------ schemas


class ReaderPatch(BaseModel):
    """A partial update. Only the fields actually sent are applied.

    `model_fields_set` is what distinguishes "theme omitted" from "theme set to
    null", which matters because the Settings page sends one field at a time.
    """

    readerType: str | None = None
    devicePrefs: dict[str, Any] | None = None
    theme: str | None = None


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


# ---------------------------------------------------------------- serialisation


def _epoch_ms(moment: datetime) -> int:
    """A stored timestamp as epoch milliseconds, whatever SQLite handed back.

    `utc_now()` writes an aware UTC datetime, but SQLite has no timestamp type and
    SQLAlchemy's format string carries no offset, so the value comes back *naive*.
    Calling `.timestamp()` on it would have Python assume the server's local zone
    and shift every session by the UTC offset — which reads on screen as sessions
    dated a day early, and only for readers west of Greenwich.
    """

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


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
def get_me(db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """The reading profile. Not the account — this server has no accounts."""

    reader = _reader(db)
    baseline = reading_speed_service.baseline_for(READER_ID)
    return {
        "wpm": round(baseline.baseline_wpm),
        "wpmIsMeasured": baseline.is_evidence,
        "readingSpeedPreset": reader.reading_speed_preset,
        "readerType": reader.reader_type,
        "devicePrefs": reader.device_prefs or {},
        "theme": reader.theme,
    }


@router.patch("/me")
def patch_me(patch: ReaderPatch, db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Update whichever preferences were sent.

    `wpm` is not settable here. A baseline is either measured from the passage or
    claimed via a preset, and both of those routes record *how* it was arrived at.
    A bare number assigned through the settings screen would lose that, and
    analytics treats a measured baseline and a claimed one differently.
    """

    reader = _reader(db)
    sent = patch.model_fields_set

    if "readerType" in sent:
        reader.reader_type = patch.readerType
    if "theme" in sent and patch.theme:
        reader.theme = patch.theme
    if "devicePrefs" in sent and patch.devicePrefs is not None:
        # Merged, not replaced: the Settings page sends one toggle at a time, and
        # replacing would silently reset the other three.
        reader.device_prefs = {**(reader.device_prefs or {}), **patch.devicePrefs}

    return get_me(db)


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
def dashboard(db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Today's reading, plus the rig's status.

    "Today" is the server's local calendar day, matching the frontend mock's
    `toDateString()` comparison. Same machine, same day; when the two are ever
    apart, this is the line that has to learn about the reader's timezone.
    """

    _reader(db)
    baseline = reading_speed_service.baseline_for(READER_ID)
    total = db.scalar(select(SessionRow.id).limit(1))

    # Compared naive-UTC, because that is what SQLite stores — see `_epoch_ms`.
    # The day boundary is the *reader's* midnight converted to UTC, not UTC
    # midnight, or "today's pages" would reset in the middle of an evening.
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    todays = list(db.scalars(select(SessionRow).where(SessionRow.created_at >= since)))

    return {
        "empty": total is None,
        "wpm": round(baseline.baseline_wpm),
        "pagesToday": sum(row.pages_read for row in todays),
        "lookupsToday": sum(row.lookup_count for row in todays),
        **device_status(),
    }


@router.get("/reading-test")
def get_reading_test() -> dict[str, Any]:
    """The passage to read, and how many words it is."""

    return {"paragraph": READING_TEST_PASSAGE, "wordCount": READING_TEST_WORDS}


@router.post("/reading-test")
def submit_reading_test(
    submission: ReadingTestSubmission, db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Measure a baseline from the timed passage.

    422 rather than a clamped number when the timing is implausible, because that
    is `calibrate_from_passage`'s deliberate choice: a stopwatch left running
    yields a baseline that looks entirely reasonable and is permanently wrong, and
    every difficulty verdict for the rest of the reader's life is measured against
    it. A visible failure the reader can retry is the cheaper outcome.
    """

    reader = _reader(db)
    try:
        baseline = reading_speed_service.calibrate(
            READER_ID, word_count=READING_TEST_WORDS, elapsed_ms=submission.elapsedMs
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
    submission: PresetSubmission, db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Record a self-reported pace, for a reader who skips the test."""

    wpm = SPEED_PRESETS.get(submission.preset)
    if wpm is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown preset '{submission.preset}'; expected one of "
            f"{', '.join(SPEED_PRESETS)}",
        )

    reader = _reader(db)
    baseline = reading_speed_service.set_manual_baseline(READER_ID, baseline_wpm=wpm)
    reader.reading_speed_preset = submission.preset
    store_baseline(reader, baseline)
    return {"wpm": round(baseline.baseline_wpm), "wpmIsMeasured": baseline.is_evidence}


# ------------------------------------------------------------- sessions & folders


@router.get("/sessions")
def list_sessions(db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """Every recorded session, newest first, with the folders to file them in."""

    rows = db.scalars(select(SessionRow).order_by(SessionRow.created_at.desc())).all()
    folders = db.scalars(select(Folder).order_by(Folder.name)).all()
    return {
        "folders": [{"id": f.id, "name": f.name} for f in folders],
        "sessions": [_session_summary_row(row) for row in rows],
    }


def _require_session(db: OrmSession, session_id: str) -> SessionRow:
    row = db.get(SessionRow, session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return row


@router.get("/sessions/{session_id}")
def session_details(session_id: str, db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    """One session in full.

    `summary` is `None` for a session read without Meaning Mode. That is not a
    failure: `AiBridge.review()` refuses to summarise a session with no lookups
    rather than inventing one from nothing, so the page needs an empty state and
    not an error.
    """

    return _session_detail(_require_session(db, session_id))


@router.patch("/sessions/{session_id}")
def patch_session(
    session_id: str, patch: SessionPatch, db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    """Rename a session, or move it into a folder (or out of all of them)."""

    row = _require_session(db, session_id)
    sent = patch.model_fields_set

    if "name" in sent and patch.name:
        row.name = patch.name
    if "folderId" in sent:
        if patch.folderId is not None and db.get(Folder, patch.folderId) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
        row.folder_id = patch.folderId

    return _session_summary_row(row)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, db: OrmSession = Depends(get_db)) -> Response:
    """Delete a session and everything hanging off it.

    Really deleted, not flagged. The reader asked; a session they cannot see but
    which still counts toward today's pages is worse than gone.
    """

    db.delete(_require_session(db, session_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def create_folder(body: FolderBody, db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    folder = Folder(name=body.name)
    db.add(folder)
    db.flush()
    return {"id": folder.id, "name": folder.name}


@router.patch("/folders/{folder_id}")
def rename_folder(
    folder_id: str, body: FolderBody, db: OrmSession = Depends(get_db)
) -> dict[str, Any]:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
    folder.name = body.name
    return {"id": folder.id, "name": folder.name}


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(folder_id: str, db: OrmSession = Depends(get_db)) -> Response:
    """Delete a folder. Its sessions survive, unfiled.

    The `ON DELETE SET NULL` on `sessions.folder_id` says the same thing at the
    schema level, and it is set explicitly here because SQLite does not enforce
    foreign keys unless asked to — relying on the constraint would leave rows
    pointing at a folder that no longer exists, and the sessions list would drop
    them silently.
    """

    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")

    for row in db.scalars(select(SessionRow).where(SessionRow.folder_id == folder_id)):
        row.folder_id = None
    db.delete(folder)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
