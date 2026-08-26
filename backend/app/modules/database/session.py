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
    """Create any missing tables and migrate existing ones. Safe to call repeatedly.

    Imports the models for their side effect of registering on `Base.metadata` —
    without it a fresh process creates nothing, because a declarative class that
    has never been imported is not in the registry.

    Migration steps are idempotent: each one checks for the current state before
    acting, so running `init_db()` twice produces the same result as running it
    once. The order matters — the `readers` table must exist before its auth
    columns can be added, and `live-reader` must exist before it can be backfilled
    as an owner.
    """

    from backend.app.modules.database import models  # noqa: F401  (registers tables)
    from sqlalchemy import text

    with engine.begin() as conn:
        # Enable Foreign Keys explicitly per connection
        conn.execute(text("PRAGMA foreign_keys = ON;"))

        # --- helpers ---
        def column_exists(table_name: str, column_name: str) -> bool:
            cursor = conn.execute(text(f"PRAGMA table_info({table_name})"))
            return any(row[1] == column_name for row in cursor.fetchall())

        def table_exists(table_name: str) -> bool:
            cursor = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table_name})
            return cursor.fetchone() is not None

        # Step 1: Ensure readers table exists (old schema, without auth columns)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS readers (
                id VARCHAR(64) PRIMARY KEY,
                created_at DATETIME,
                updated_at DATETIME,
                baseline_payload JSON,
                reading_speed_preset VARCHAR(16),
                reader_type VARCHAR(32),
                device_prefs JSON,
                theme VARCHAR(16)
            )
        """))

        # Step 2: Add auth columns to readers if missing (Phase 2 migration gap)
        # Each column is checked individually so the migration works whether zero,
        # some, or all columns are already present.
        _AUTH_COLUMNS = {
            "name": "VARCHAR(255)",
            "email": "VARCHAR(255)",
            "password_hash": "VARCHAR(255)",
            "profile_completed": "INTEGER DEFAULT 0",
            "session_token": "VARCHAR(64)",
        }
        for col_name, col_type in _AUTH_COLUMNS.items():
            if not column_exists("readers", col_name):
                conn.execute(text(f"ALTER TABLE readers ADD COLUMN {col_name} {col_type}"))

        # Step 3: Ensure live-reader exists
        conn.execute(text("""
            INSERT OR IGNORE INTO readers (id) VALUES ('live-reader')
        """))

        # Step 4: Safe demo-account initialization for live-reader.
        # Populate demo credentials only when the identity fields are empty, so
        # a real user who has signed up as live-reader (or changed the demo
        # password) is never silently overwritten on the next server restart.
        row = conn.execute(text(
            "SELECT email, name, password_hash FROM readers WHERE id = 'live-reader'"
        )).fetchone()
        if row is not None:
            current_email, current_name, current_hash = row
            if not current_hash:
                # No password set — this is the prototype seed account.
                # Import here to avoid a circular dependency and to keep the
                # hashing library out of the module-level import chain.
                from pwdlib import PasswordHash
                _pwd = PasswordHash.recommended()
                demo_hash = _pwd.hash("demo1234")
                # Only fill in email/name when they are empty too.
                new_email = current_email if current_email else "demo@taletrace.app"
                new_name = current_name if current_name else "Demo Reader"
                conn.execute(text(
                    "UPDATE readers SET email = :email, name = :name, password_hash = :hash "
                    "WHERE id = 'live-reader'"
                ), {"email": new_email, "name": new_name, "hash": demo_hash})

        # Step 5: Add reader_id to sessions/folders if missing
        if table_exists("sessions") and not column_exists("sessions", "reader_id"):
            conn.execute(text("ALTER TABLE sessions ADD COLUMN reader_id VARCHAR(64)"))

        if table_exists("folders") and not column_exists("folders", "reader_id"):
            conn.execute(text("ALTER TABLE folders ADD COLUMN reader_id VARCHAR(64)"))

        # Step 6: Backfill existing rows to live-reader
        if table_exists("sessions"):
            conn.execute(text("UPDATE sessions SET reader_id = 'live-reader' WHERE reader_id IS NULL"))
        if table_exists("folders"):
            conn.execute(text("UPDATE folders SET reader_id = 'live-reader' WHERE reader_id IS NULL"))

        # Step 7: Verify no NULL ownership
        if table_exists("sessions"):
            null_sessions = conn.execute(text("SELECT COUNT(*) FROM sessions WHERE reader_id IS NULL")).scalar()
            assert null_sessions == 0, f"Found {null_sessions} sessions with NULL reader_id"
        if table_exists("folders"):
            null_folders = conn.execute(text("SELECT COUNT(*) FROM folders WHERE reader_id IS NULL")).scalar()
            assert null_folders == 0, f"Found {null_folders} folders with NULL reader_id"

    # Step 8: Remove unused legacy tables
    with engine.begin() as conn:
        for table in ["ocr_results", "selected_words", "ai_responses", "flashcards", "quizzes", "reading_statistics"]:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))

    # Final: create_all picks up any remaining tables the model declares
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
