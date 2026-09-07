"""SQLite store. One file, WAL, no ORM.

The schema is deliberately small. Everything Turnout knows about an event
lives in four tables, and the only table holding personal data is `signup` —
which matters, because "delete everything about these people" has to be a
single statement we can be confident about.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("TURNOUT_DB", "data/turnout.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS event (
    id            TEXT PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    strap         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    starts_at     TEXT NOT NULL,              -- ISO8601 local wall time
    ends_at       TEXT,
    timezone      TEXT NOT NULL DEFAULT 'Europe/London',
    venue         TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    accessibility TEXT NOT NULL DEFAULT '',
    capacity      INTEGER,                    -- NULL = uncapped (listing only)
    oversell_pct  INTEGER NOT NULL DEFAULT 0,
    capacity_mode TEXT NOT NULL DEFAULT 'pool' CHECK (capacity_mode IN ('pool','allocated')),
    contact       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','live','closed','cancelled')),
    published     TEXT,                       -- JSON snapshot of last pushed version
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,               -- manual | eventbrite | luma | atproto | facebook
    label        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    external_id  TEXT,                        -- platform's event id
    allocation   INTEGER,                     -- used in 'allocated' mode
    credential_id TEXT,                       -- which saved access to use
    policy       TEXT NOT NULL DEFAULT 'waitlist'
                 CHECK (policy IN ('waitlist','close','open')),
    state        TEXT NOT NULL DEFAULT 'idle'
                 CHECK (state IN ('idle','live','drift','closed','manual','failed')),
    show_on_list INTEGER NOT NULL DEFAULT 1,
    config       TEXT NOT NULL DEFAULT '{}',  -- JSON, adapter-specific
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS channel_event ON channel(event_id);

-- The only table holding personal data.
CREATE TABLE IF NOT EXISTS signup (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    channel_id  TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,                -- platform's attendee/order id
    name        TEXT NOT NULL DEFAULT '',
    email       TEXT,                         -- NULL where the platform won't give one
    handle      TEXT,                         -- for platforms with handles, not emails
    status      TEXT NOT NULL DEFAULT 'going'
                CHECK (status IN ('going','waitlist','cancelled')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    UNIQUE (channel_id, external_id)
);
CREATE INDEX IF NOT EXISTS signup_event ON signup(event_id, status);

CREATE TABLE IF NOT EXISTS log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL,
    channel_id TEXT,
    code       TEXT NOT NULL DEFAULT '--',    -- short channel code for display
    at         TEXT NOT NULL,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_event ON log(event_id, id DESC);

-- Credentials are kept apart from event data so they can be stored
-- differently later (env, vault, KMS) without touching the rest.
CREATE TABLE IF NOT EXISTS credential (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,                 -- eventbrite | luma | atproto
    label      TEXT NOT NULL DEFAULT '',
    secret     TEXT NOT NULL,
    config     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


# Small forward migrations, applied on every connect. Cheap, idempotent, and
# it means an existing database keeps working across an upgrade.
MIGRATIONS = [
    ("channel", "credential_id", "ALTER TABLE channel ADD COLUMN credential_id TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def last_log_id(conn: sqlite3.Connection, event_id: str) -> int:
    """The high-water mark of the log, so an action can say what it just did.

    Actions redirect after they run — which is right, a refresh must not
    publish twice — and that throws the per-channel report away. Taking this
    before the action and passing it back in the URL lets the page render
    exactly the lines that action wrote, and nothing older.
    """
    row = conn.execute("SELECT MAX(id) AS m FROM log WHERE event_id = ?",
                       (event_id,)).fetchone()
    return (row["m"] if row else None) or 0


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def log(conn: sqlite3.Connection, event_id: str, code: str, message: str,
        channel_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO log (event_id, channel_id, code, at, message) VALUES (?,?,?,?,?)",
        (event_id, channel_id, code, now(), message),
    )
    conn.commit()
