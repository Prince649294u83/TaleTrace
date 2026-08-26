"""Where the SQLite file lives, and how a caller gets a transaction.

Two callers, two shapes, and the split is deliberate:

    get_db()        FastAPI dependency — one transaction per request
    db_session()    context manager — for `live_session` and `simulated_session`,
                    which are not requests and have no dependency injection

Both hand out the same `sessionmaker`, so a row written by the rig and a row read
by the website cannot disagree about the schema.

`create_all` rather than Alembic
--------------------------------
One developer, one file, no deployed instance to migrate. `create_all` is
additive — it creates missing tables and leaves existing ones alone — which is
exactly right until the first column changes type. When that happens the answer
is to delete `taletrace.db` and read another session, not to add a migration
tool for a database with no users in it. If this ever ships to a second machine,
that is the moment Alembic earns its place.

`check_same_thread=False`
-------------------------
FastAPI runs `def` endpoints in a threadpool, so the connection that opens a
session is routinely not the thread that uses it. SQLite's default refusal is a
guard against sharing one connection across threads *concurrently*; the pool
hands out one connection per session, so the guard protects nothing here and
only produces spurious failures.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from backend.app.config.settings import get_settings
from backend.app.modules.database.base import Base

logger = logging.getLogger(__name__)

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
    future=True,
)

SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables. Safe to call more than once.

    Imports the models for their side effect of registering on `Base.metadata` —
    without it a fresh process creates nothing, because a declarative class that
    has never been imported is not in the registry.
    """

    from backend.app.modules.database import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(bind=engine)
    logger.debug("database ready at %s", _settings.database_url)


@contextmanager
def db_session() -> Iterator[OrmSession]:
    """A transaction for code that is not a request. Commits, or rolls back.

    Used by the two session runners. They are long-lived processes that write
    exactly once, at the end, and a half-written session row is worse than no
    row: the website would show a reading with real pages and no summary and no
    way to tell that it was truncated.
    """

    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[OrmSession]:
    """FastAPI dependency. One transaction per request, rolled back on error.

    Separate from `db_session` rather than wrapping it because FastAPI needs a
    generator it can drive itself, and because a `HTTPException` raised in a
    route must not be swallowed by a context manager's `except`.
    """

    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
