"""SQLite database layer for AgentTrafficCop.

Handles user accounts and API keys. Uses SQLite for local development.
When moving to production, swap the connection string to Postgres and
change `sqlite3` to `psycopg` (or use SQLAlchemy).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("agenttrafficcop.db")

# Put the database file at the repo root for local dev.
DB_PATH = Path(__file__).resolve().parent.parent / "agenttrafficcop.db"


def init_db() -> None:
    """Create the tables if they don't exist."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # API Keys table
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Access columns by name
    try:
        yield conn
    finally:
        conn.close()


# Initialize the DB immediately on import
init_db()