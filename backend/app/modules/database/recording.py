"""The seam between a session and the database, in both directions.

Out: `record_session` turns a finished session into a row, called once, at the
only moment when everything exists at the same time — `DeviceLoop.run()` has
returned `SessionAnalytics` and the engine is still holding the review, the focus
report and the lookup list from `finish_session`. Nothing here computes a reading
metric — every number is assigned straight from the value a module already
produced. If a figure looks wrong on the website, it is wrong in the module that
measured it, not here.

In: `hydrate_reading_speed` loads the reader's stored baseline back into a
`ReadingSpeedService` before a session starts. That direction lives here rather
than in the API because the rig and the simulator need it too and neither can
import a FastAPI router — and because `READER_ID` is a `readers` row id, so the
three programs that name that row belong to one constant, not three.

Ownership
---------
Every session and folder carries a `reader_id` foreign key back to the `readers`
table. The rig and the simulator write as `READER_ID` (``live-reader``); the
website creates additional readers via signup and filters every query by the
authenticated reader's id.

No per-page rows. `analytics.pages` carries a `DifficultyMetrics` per page with
its evidence, and the website has nowhere to show it — the Session Details page
displays one difficulty word. It travels inside `focus_payload`'s sibling blob if
a page for it is ever built; until then a table per page is storage with no
reader.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from backend.app.modules.database.models import Reader
from backend.app.modules.database.models import Session as SessionRow
from backend.app.modules.database.models import utc_now
from backend.app.modules.reading_speed.models import (
    DifficultyLevel,
    ReadingBaseline,
    SessionAnalytics,
)
from backend.app.modules.reading_speed.service import (
    ReadingSpeedService,
    reading_speed_service,
)

logger = logging.getLogger(__name__)

# The one local reader. Every session on this device is read by the same person,
# so this is a constant rather than a parameter — but it is a `readers` row id,
# and the API, the rig and the simulator have to name the *same* row or a
# calibration taken in the browser reaches nobody. It lived in `live_session`
# and was copied, differently, into `simulated_session`; one home is what stops
# that happening again. A second reader turns this into a lookup, not into three
# copies of a string.
READER_ID = "live-reader"

# Worst first. Used to break a tie between page verdicts, and to keep the
# ordering in one place rather than implied by a dict literal somewhere.
_SEVERITY = (DifficultyLevel.HIGH, DifficultyLevel.MEDIUM, DifficultyLevel.LOW)


def store_baseline(reader: Reader, baseline: ReadingBaseline) -> None:
    """Keep the durable copy in step with the in-memory one.

    The whole baseline, not just its wpm: `method` and `calibrated` decide
    `is_evidence`, and analytics refuses to infer difficulty from a baseline that
    is only a claim. Persisting the number alone would have every restart present
    a guess as a measurement.
    """

    reader.baseline_payload = baseline.model_dump(mode="json")


def hydrate_reading_speed(service: ReadingSpeedService | None = None) -> ReadingSpeedService:
    """Load the reader's stored baseline into `service`, and return it.

    Called at the start of every program that runs or reports on a session: the
    API on startup, and both session runners before they build a runtime. Without
    it a calibration made in the browser reaches only the browser — the rig
    measures the reader against the default 200 wpm, `is_evidence` is False, and
    `assess_page` declines to rate any page at all. On screen that is a session
    whose difficulty is permanently blank, which reads as a broken tile rather
    than as the missing measurement it is.

    Returns the service so the caller can hand the same instance to
    `ReadingRuntime.build`. That is the part that is easy to get wrong: `build`
    constructs its own `ReadingSpeedService` when it is not given one, so
    hydrating a service and then not passing it on is indistinguishable from not
    hydrating at all.

    Defaults to the process-wide instance, which is what the API and the rig
    both want. The simulator passes its own, because its service has to share the
    virtual clock or every wpm it computes is wrong by the speed factor.

    Failures are logged, not raised. A run that will not start because a baseline
    could not be read is worse than one that starts with the default: the default
    is marked DEFAULT and every consumer can see it is an assumption.
    """

    target = service if service is not None else reading_speed_service
    try:
        # Imported here, as in `record_finished_session`: importing the session
        # module creates the engine, and neither the API nor a test should touch
        # the database file merely by importing this one.
        from backend.app.modules.database.session import SessionFactory, init_db

        init_db()  # a first run on a fresh checkout has no `readers` table yet
        with SessionFactory() as db:
            reader = db.get(Reader, READER_ID)
            if reader is None or not reader.baseline_payload:
                return target
            baseline = ReadingBaseline.model_validate(reader.baseline_payload)
            target.restore_baseline(baseline)
            logger.info(
                "[database] restored %s baseline %.1f wpm (%s) for %s",
                "measured" if baseline.is_evidence else "assumed",
                baseline.baseline_wpm,
                baseline.method.value,
                READER_ID,
            )
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning("[database] could not restore baseline: %s", exc)
    return target


def session_difficulty(analytics: SessionAnalytics) -> DifficultyLevel:
    """One verdict for a whole session, from the per-page verdicts.

    The most common rating across the pages that got one, ties going to the
    harder of them — a session split evenly between hard and easy pages was not
    an easy read.

    Reuses `assess_page`'s conclusions rather than re-thresholding
    `average_deviation_ratio`, because that average has already discarded what
    makes the page verdicts trustworthy: a slow page with no lookups and no
    re-reads is an interruption, not difficulty, and `assess_page` is the code
    that knows the difference. Averaging the ratios would quietly let a phone
    call count as a hard book.

    Pages rated UNKNOWN are excluded, and a session with nothing but UNKNOWN
    pages stays UNKNOWN — which the API renders as no difficulty at all rather
    than as "Medium".
    """

    counts = Counter(
        page.difficulty for page in analytics.pages if page.difficulty is not DifficultyLevel.UNKNOWN
    )
    if not counts:
        return DifficultyLevel.UNKNOWN
    return max(counts, key=lambda level: (counts[level], -_SEVERITY.index(level)))


def record_session(
    db: OrmSession,
    *,
    analytics: SessionAnalytics,
    focus_report: Any = None,
    review: Any = None,
    lookups: list[str] | None = None,
    name: str = "Reading Session",
    source_reference: str | None = None,
) -> SessionRow:
    """Write one finished session and return the row.

    `review` and `focus_report` are typed `Any` for the same reason the engine's
    properties are: a `None` review is normal. `AiBridge.review()` refuses a
    session with no lookups, and an unreachable Groq is a failed `AiOutcome`, not
    an exception. Both cases must still produce a row — a session read without
    Meaning Mode is a real session, it just has no summary.

    Does not commit. The caller owns the transaction, so the rig can write this
    row inside `db_session()` and a test can write one and roll it back.
    """

    ok = bool(review is not None and getattr(review, "ok", False))
    payload: dict[str, Any] = dict(getattr(review, "data", {}) or {}) if ok else {}

    row = SessionRow(
        status="completed",
        ended_at=utc_now(),
        name=name,
        source_reference=source_reference,
        baseline_wpm=analytics.baseline_wpm,
        session_wpm=analytics.session_wpm,
        words_read=analytics.words_read,
        pages_read=analytics.pages_read,
        reading_duration_ms=analytics.reading_duration_ms,
        wall_duration_ms=analytics.wall_duration_ms,
        lookup_count=analytics.lookup_count,
        meaning_requests=analytics.meaning_requests,
        difficulty=session_difficulty(analytics).value,
        summary=payload.get("session_summary") or None,
        review_payload=payload,
        focus_payload=(
            focus_report.model_dump(mode="json") if hasattr(focus_report, "model_dump") else {}
        ),
        lookups=list(lookups or ()),
    )
    db.add(row)
    db.flush()  # so the caller can log the id without committing

    logger.info(
        "[database] recorded session %s pages=%d words=%d wpm=%.1f difficulty=%s summary=%s",
        row.id,
        row.pages_read,
        row.words_read,
        row.session_wpm,
        row.difficulty,
        "yes" if row.summary else "no",
    )
    return row


def record_finished_session(
    engine: Any,
    analytics: SessionAnalytics,
    *,
    name: str = "Reading Session",
    source_reference: str | None = None,
) -> tuple[str | None, str]:
    """Save what a session runner just finished. Returns `(row_id, note)`.

    Everything this needs is already on the engine — `finish_session` set
    `review` and `focus_report` on its way to returning the analytics — so the
    two runners call this with the loop's return value and nothing else.

    Returns rather than raises, and the reason is the reader: the session already
    happened. A locked database file must not turn a good twenty-minute read into
    a traceback and no printed page count. The runner prints the note, so a
    failure is visible and attributable, never silent — but the numbers still
    reach the person who was holding the book.
    """

    from backend.app.modules.database.session import db_session, init_db

    try:
        init_db()
        with db_session() as db:
            row = record_session(
                db,
                analytics=analytics,
                focus_report=engine.focus_report,
                review=engine.review,
                lookups=engine.lookups,
                name=name,
                source_reference=source_reference,
            )
            return row.id, "saved"
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning("[database] could not record session: %s", exc)
        return None, f"not saved ({exc})"
