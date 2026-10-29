"""SQLite database layer for AgentTrafficCop.

Handles user accounts and API keys. Uses SQLite for local development.
When moving to production, swap the connection string to Postgres and
change `sqlite3` to `psycopg` (or use SQLAlchemy).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("agenttrafficcop.db")

# Use ATC_DB_PATH from environment if available (for Render persistent disks),
# otherwise default to the local repo root.
_db_path_env = os.getenv("ATC_DB_PATH")
if _db_path_env:
    DB_PATH = Path(_db_path_env)
else:
    DB_PATH = Path(__file__).resolve().parent.parent / "agenttrafficcop.db"

# FIX: the original did a bare `DB_PATH.parent.mkdir(...)` at import time.
# If the parent dir isn't writable (Render disk not mounted, read-only FS,
# permission error), the process died with a raw PermissionError and no
# log line explaining why. Wrap it, emit a critical log with a hint, then
# still raise — crashing is correct here, but now it's diagnosable.
try:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logger.critical(
        "db_parent_dir_unavailable path=%s error=%s "
        "hint='On Render, mount a persistent disk and set "
        "ATC_DB_PATH=/data/agenttrafficcop.db'",
        DB_PATH.parent,
        e,
    )
    raise


def init_db() -> None:
    """Create the tables if they don't exist."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # key_hash stores the SHA-256 hash of the key, never the raw key.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                tier TEXT NOT NULL DEFAULT 'free',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)

        conn.commit()
        logger.info("database_initialized path=%s", DB_PATH)


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections.

    Yields:
        sqlite3.Connection: A connection with row_factory set to sqlite3.Row.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# Initialize the DB immediately on import
init_db()