"""SQLite persistence layer for the Feature Flag Service.

A single connection is created per Flask app instance (rather than
per-request) so that ``sqlite3.connect(":memory:")`` works correctly for
tests -- an in-memory database only lives as long as its connection, and a
fresh per-request connection would see an empty database on every request.
"""

import sqlite3
import threading

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    default_value TEXT NOT NULL DEFAULT 'false'
);

CREATE TABLE IF NOT EXISTS targeting_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    rule_config TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evaluation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id INTEGER NOT NULL,
    user_id TEXT,
    result TEXT,
    evaluated_at TEXT NOT NULL
);
"""


def init_db(app):
    """Create the sqlite connection for this app and ensure schema exists."""
    conn = sqlite3.connect(
        app.config["DATABASE"],
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    app.extensions["ffs_db_conn"] = conn
    app.extensions["ffs_db_lock"] = threading.Lock()


def get_db():
    """Return the shared sqlite connection for the current app."""
    return current_app.extensions["ffs_db_conn"]


def get_lock():
    return current_app.extensions["ffs_db_lock"]
