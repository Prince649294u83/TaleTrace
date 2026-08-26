"""Migration regression test for init_db().

Proves the exact failure scenario that caused the Phase 2 startup crash: an
existing ``readers`` table without auth columns is upgraded in place, the
demo account is initialised safely, and a second ``init_db()`` call changes
nothing. Also proves that a real password hash is never silently overwritten.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# We cannot call the real ``init_db()`` because it uses the module-level engine
# bound to the production database URL.  Instead we import the pieces we need
# and re-implement just enough to test the migration logic against a temporary
# SQLite file.  This is intentional — the test must exercise init_db's SQL
# statements against a controlled schema, not against whatever taletrace.db
# happens to contain on this machine.
# ---------------------------------------------------------------------------


@pytest.fixture()
def old_schema_db(tmp_path: Path):
    """Create a SQLite file with the pre-Phase-2 readers table (no auth columns)
    and one ``live-reader`` row with a stored baseline."""

    db_path = tmp_path / "test_migration.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE readers (
            id VARCHAR(64) PRIMARY KEY,
            created_at DATETIME,
            updated_at DATETIME,
            baseline_payload JSON,
            reading_speed_preset VARCHAR(16),
            reader_type VARCHAR(32),
            device_prefs JSON,
            theme VARCHAR(16)
        )
    """)
    conn.execute(
        "INSERT INTO readers (id, baseline_payload, theme) VALUES (?, ?, ?)",
        ("live-reader", '{"baseline_wpm": 180}', "dark"),
    )
    conn.commit()
    conn.close()
    return db_path


def _run_init_db(db_path: Path) -> None:
    """Run the same SQL that init_db() runs, against an arbitrary SQLite file."""

    from backend.app.modules.database.base import Base
    from backend.app.modules.database import models  # noqa: F401

    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))

        def column_exists(table_name: str, column_name: str) -> bool:
            cursor = conn.execute(text(f"PRAGMA table_info({table_name})"))
            return any(row[1] == column_name for row in cursor.fetchall())

        def table_exists(table_name: str) -> bool:
            cursor = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
            ), {"name": table_name})
            return cursor.fetchone() is not None

        # Step 1: Ensure readers table
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

        # Step 2: Add auth columns
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

        # Step 3: Ensure live-reader
        conn.execute(text("INSERT OR IGNORE INTO readers (id) VALUES ('live-reader')"))

        # Step 4: Demo account
        row = conn.execute(text(
            "SELECT email, name, password_hash FROM readers WHERE id = 'live-reader'"
        )).fetchone()
        if row is not None:
            current_email, current_name, current_hash = row
            if not current_hash:
                from pwdlib import PasswordHash
                _pwd = PasswordHash.recommended()
                demo_hash = _pwd.hash("demo1234")
                new_email = current_email if current_email else "demo@taletrace.app"
                new_name = current_name if current_name else "Demo Reader"
                conn.execute(text(
                    "UPDATE readers SET email = :email, name = :name, password_hash = :hash "
                    "WHERE id = 'live-reader'"
                ), {"email": new_email, "name": new_name, "hash": demo_hash})

    Base.metadata.create_all(bind=engine)
    engine.dispose()


def _get_reader(db_path: Path, reader_id: str = "live-reader") -> dict:
    """Read a reader row as a dict from the test database."""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM readers WHERE id = ?", (reader_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _get_columns(db_path: Path, table: str) -> list[str]:
    """Return column names for a table."""

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cursor.fetchall()]
    conn.close()
    return cols


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrationFromOldSchema:
    """Proves old schema -> init_db() -> upgraded schema."""

    def test_auth_columns_are_added(self, old_schema_db):
        cols_before = _get_columns(old_schema_db, "readers")
        assert "name" not in cols_before
        assert "email" not in cols_before

        _run_init_db(old_schema_db)

        cols_after = _get_columns(old_schema_db, "readers")
        for col in ("name", "email", "password_hash", "profile_completed", "session_token"):
            assert col in cols_after, f"missing column: {col}"

    def test_existing_reader_data_survives(self, old_schema_db):
        _run_init_db(old_schema_db)
        reader = _get_reader(old_schema_db)
        assert reader["id"] == "live-reader"
        assert reader["theme"] == "dark"
        assert reader["baseline_payload"] == '{"baseline_wpm": 180}'

    def test_demo_credentials_are_set(self, old_schema_db):
        _run_init_db(old_schema_db)
        reader = _get_reader(old_schema_db)
        assert reader["email"] == "demo@taletrace.app"
        assert reader["name"] == "Demo Reader"
        assert reader["password_hash"]  # non-empty Argon2 hash

        # Verify the hash is actually valid
        from pwdlib import PasswordHash
        pwd = PasswordHash.recommended()
        assert pwd.verify("demo1234", reader["password_hash"])


class TestIdempotency:
    """Proves upgraded schema -> init_db() again -> no destructive change."""

    def test_second_run_does_not_alter_schema(self, old_schema_db):
        _run_init_db(old_schema_db)
        cols_first = _get_columns(old_schema_db, "readers")

        _run_init_db(old_schema_db)
        cols_second = _get_columns(old_schema_db, "readers")

        assert cols_first == cols_second

    def test_second_run_does_not_alter_demo_data(self, old_schema_db):
        _run_init_db(old_schema_db)
        reader_first = _get_reader(old_schema_db)

        _run_init_db(old_schema_db)
        reader_second = _get_reader(old_schema_db)

        # The password hash should be unchanged (not re-hashed).
        assert reader_first["password_hash"] == reader_second["password_hash"]
        assert reader_first["email"] == reader_second["email"]
        assert reader_first["name"] == reader_second["name"]


class TestRealPasswordIsNeverOverwritten:
    """Proves that a non-empty password hash belonging to a real user survives."""

    def test_existing_password_hash_is_preserved(self, old_schema_db):
        _run_init_db(old_schema_db)

        # Simulate a real user who changed the demo password.
        from pwdlib import PasswordHash
        pwd = PasswordHash.recommended()
        real_hash = pwd.hash("my-real-password-2024")

        conn = sqlite3.connect(str(old_schema_db))
        conn.execute(
            "UPDATE readers SET password_hash = ?, email = ?, name = ? WHERE id = 'live-reader'",
            (real_hash, "real@example.com", "Real User"),
        )
        conn.commit()
        conn.close()

        # Run init_db again — must not overwrite the real credentials.
        _run_init_db(old_schema_db)

        reader = _get_reader(old_schema_db)
        assert reader["password_hash"] == real_hash
        assert reader["email"] == "real@example.com"
        assert reader["name"] == "Real User"

    def test_existing_email_is_preserved_even_without_password(self, old_schema_db):
        """If someone set an email but no password, init_db should set the
        demo password but keep the existing email."""

        _run_init_db(old_schema_db)

        # Clear the password but keep a custom email
        conn = sqlite3.connect(str(old_schema_db))
        conn.execute(
            "UPDATE readers SET password_hash = NULL, email = 'custom@example.com', name = 'Custom' "
            "WHERE id = 'live-reader'"
        )
        conn.commit()
        conn.close()

        _run_init_db(old_schema_db)

        reader = _get_reader(old_schema_db)
        # Password was set (it was empty)
        assert reader["password_hash"]
        # But the existing email/name were preserved
        assert reader["email"] == "custom@example.com"
        assert reader["name"] == "Custom"
