"""Shared pytest fixtures for the AgentTrafficCop test suite.

Design:
  * Tests run in-process via FastAPI's TestClient — no real server.
  * The session backend is forced to in-memory for speed and isolation.
  * Each test gets a fresh session store and a fresh TestClient.
  * Auth is always configured with a known key so tests exercise the
    real security path (not the fail-closed-without-keys path).
"""

from __future__ import annotations

import os

import pytest

# --- Environment setup -------------------------------------------------------
# These must be set BEFORE importing app.* so that app.config.Settings
# picks them up at import time. pytest loads conftest.py before collecting
# test modules, so this runs first.
os.environ["ATC_API_KEYS"] = "test-key-1,test-key-2"
os.environ["ATC_ENV"] = "development"
os.environ["ATC_LOG_LEVEL"] = "WARNING"
# Force in-memory backend so tests don't need Redis running.
os.environ.pop("ATC_REDIS_URL", None)

# --- Imports after env setup -------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import session_store  # noqa: E402


# --- Constants used by tests -------------------------------------------------

VALID_API_KEY = "test-key-1"
SECOND_API_KEY = "test-key-2"
INVALID_API_KEY = "not-a-real-key"


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def client():
    """A FastAPI TestClient bound to the app.

    Fresh per test. Uses the app as-is; the app is already configured
    by the environment variables set above.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_sessions():
    """Reset the session store before and after every test.

    Ensures no cross-test state leakage. autouse=True means this runs
    automatically for every test without needing to be requested.
    """
    # Best-effort reset. InMemoryBackend exposes a private dict; we
    # clear it via the public interface by popping all known sessions.
    # (A cleaner reset method could be added to the backend later.)
    if hasattr(session_store, "_sessions"):
        with session_store._lock:  # type: ignore[attr-defined]
            session_store._sessions.clear()  # type: ignore[attr-defined]

    yield

    if hasattr(session_store, "_sessions"):
        with session_store._lock:  # type: ignore[attr-defined]
            session_store._sessions.clear()  # type: ignore[attr-defined]


@pytest.fixture()
def auth_headers():
    """Authorization header with a valid key."""
    return {"authorization": f"Bearer {VALID_API_KEY}"}


@pytest.fixture()
def sample_message():
    """A valid AgentMessage payload as a dict."""
    return {
        "session_id": "test-session",
        "sender": "coder",
        "recipient": "reviewer",
        "intent": "handoff",
        "payload": {"diff": "fix typo", "file": "app.py"},
    }