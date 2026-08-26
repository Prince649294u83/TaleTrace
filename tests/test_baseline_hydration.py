"""The stored baseline has to reach a session, not just the API.

The bug this guards: a reader calibrated at 216.7 wpm in the browser, and every
page of every session afterwards rated `unknown`. Hydration existed, but only in
the FastAPI lifespan — so the value lived in the web process and the rig, which is
the thing that actually measures reading, started every session at the default
200 wpm with `is_evidence` False. `analytics._classify` then declines to rate any
page at all, and the website shows a session whose difficulty is permanently
blank. Nothing errors anywhere along that path.

Two links, because they break separately: `hydrate_reading_speed` reading the row
into a service, and a session runner handing that same service to the runtime.
The second is the one that looks fine in review — `ReadingRuntime.build`
constructs its own `ReadingSpeedService` when it is not given one, so hydrating a
service and forgetting to pass it on is indistinguishable from never hydrating.

The database is a temp in-memory SQLite substituted for the module's own factory,
with `init_db` stubbed so the real `taletrace.db` is never touched. `StaticPool`
because the default pool would hand each connection its own private in-memory
database and the reader row would vanish between writing and reading it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.modules.database import session as session_module
from backend.app.modules.database.base import Base
from backend.app.modules.database.models import Reader
from backend.app.modules.database.recording import READER_ID, hydrate_reading_speed
from backend.app.modules.reading_speed.models import CalibrationMethod, ReadingBaseline
from backend.app.modules.reading_speed.service import ReadingSpeedService

MEASURED_WPM = 216.7


@pytest.fixture
def stored_baseline(monkeypatch):
    """A `readers` row holding a measured baseline, as the reading test writes it."""

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    baseline = ReadingBaseline(
        reader_id=READER_ID,
        baseline_wpm=MEASURED_WPM,
        calibrated=True,
        method=CalibrationMethod.MEASURED,
        sample_count=1,
    )
    with factory() as db:
        db.add(
            Reader(
                id=READER_ID,
                device_prefs={},
                baseline_payload=baseline.model_dump(mode="json"),
            )
        )
        db.commit()

    # Both patched on the module, because `hydrate_reading_speed` imports them
    # from it at call time rather than at import time.
    monkeypatch.setattr(session_module, "SessionFactory", factory)
    monkeypatch.setattr(session_module, "init_db", lambda: None)
    yield baseline
    engine.dispose()


def test_hydration_loads_the_measured_baseline(stored_baseline):
    """The number *and* its provenance: `is_evidence` is what unlocks rating."""

    service = hydrate_reading_speed(ReadingSpeedService())
    baseline = service.baseline_for(READER_ID)

    assert baseline.baseline_wpm == MEASURED_WPM
    assert baseline.is_evidence, "a baseline restored as a guess leaves every page unrated"


def test_a_simulated_session_reads_that_baseline(stored_baseline, tmp_path):
    """`build_session` must hand its hydrated service to the runtime it builds.

    Asserted through `engine.speed` rather than by running a session, because the
    failure is structural: the runtime either got the hydrated instance or built
    its own. A page of bytes is enough — nothing here reaches OCR.
    """

    from backend.app.simulated_session import build_session

    page = tmp_path / "page_01.png"
    page.write_bytes(b"not an image, never decoded")

    loop, _clock = build_session([page], speed=0.0, offline=True)
    baseline = loop.runtime.engine.speed.baseline_for(READER_ID)

    assert baseline.baseline_wpm == MEASURED_WPM
    assert baseline.is_evidence


def test_hydration_survives_an_unreadable_database(monkeypatch):
    """A session must still start. The default is marked DEFAULT and stays honest.

    Refusing to run because a baseline could not be read would cost the reader a
    session over a locked file; starting with a baseline that is visibly a guess
    costs them the difficulty ratings for one run.
    """

    def explode() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(session_module, "init_db", explode)

    service = hydrate_reading_speed(ReadingSpeedService())

    assert not service.baseline_for(READER_ID).is_evidence
