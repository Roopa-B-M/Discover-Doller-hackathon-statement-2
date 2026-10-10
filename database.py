"""SQLite access layer: schema, connections and transaction helpers.

Every connection is opened in autocommit mode (isolation_level=None) so that
transactions are controlled explicitly with BEGIN IMMEDIATE / COMMIT / ROLLBACK.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "shiftswap.db")


def resolve_db_path(db_path=None):
    """Explicit argument > SHIFTSWAP_DB env var > data/shiftswap.db."""
    return str(db_path or os.environ.get("SHIFTSWAP_DB") or DEFAULT_DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('STAFF', 'MANAGER'))
);
CREATE TABLE IF NOT EXISTS staff_certifications (
    staff_id      TEXT NOT NULL,
    certification TEXT NOT NULL,
    PRIMARY KEY (staff_id, certification),
    FOREIGN KEY (staff_id) REFERENCES staff(id)
);
CREATE TABLE IF NOT EXISTS shifts (
    id                     TEXT PRIMARY KEY,
    start                  TEXT NOT NULL,
    "end"                  TEXT NOT NULL,
    required_certification TEXT NULL,
    assigned_to            TEXT NOT NULL,
    FOREIGN KEY (assigned_to) REFERENCES staff(id)
);
CREATE TABLE IF NOT EXISTS listings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id   TEXT NOT NULL,
    poster_id  TEXT NOT NULL,
    claimer_id TEXT NULL,
    status     TEXT NOT NULL CHECK(status IN ('OPEN','CLAIMED','WITHDRAWN','APPROVED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (shift_id)   REFERENCES shifts(id),
    FOREIGN KEY (poster_id)  REFERENCES staff(id),
    FOREIGN KEY (claimer_id) REFERENCES staff(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_listing_per_shift
    ON listings(shift_id) WHERE status IN ('OPEN', 'CLAIMED');
CREATE TABLE IF NOT EXISTS action_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    shift_id   TEXT NULL,
    outcome    TEXT NOT NULL CHECK(outcome IN ('SUCCESS', 'FAILURE')),
    details    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_connection(db_path=None):
    """Open a new connection (one per service call - safe for Streamlit reruns)."""
    path = resolve_db_path(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)


def now_str():
    """Wall-clock timestamp for DISPLAY/audit only. Never used for eligibility."""
    return datetime.now().isoformat(timespec="seconds")


def safe_rollback(conn):
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        safe_rollback(conn)
        raise
