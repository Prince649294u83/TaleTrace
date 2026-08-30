# 07. Database Findings

## Schema Integrity & Migrations

### Evidence
The SQLite migration adds reader identity with:
```sql
ALTER TABLE sessions ADD COLUMN reader_id VARCHAR(64)
```
### Analysis
The SQLite `ALTER TABLE ADD COLUMN` command does not support appending `REFERENCES` (Foreign Key) constraints on existing tables. While the SQLAlchemy model specifies `ForeignKey("readers.id")`, the actual SQLite database schema for upgraded databases completely lacks this enforcement.

### Additional Findings
- **PRAGMA foreign_keys = ON**: This PRAGMA is executed by `init_db()` on its local migration connection but is not guaranteed globally for standard application connections. SQLite defaults to OFF unless explicitly enabled per connection, completely circumventing database integrity.
- **Constraint Upgrades**: Other standard constraints (`NOT NULL`, `UNIQUE(email)`) are not properly retrofitted during raw SQL schema updates.
