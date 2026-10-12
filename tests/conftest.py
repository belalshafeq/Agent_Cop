"""Shared pytest fixtures for the AgentTrafficCop test suite.

Design:
  * Tests run in-process via FastAPI's TestClient — no real server.
  * The session backend is forced to in-memory for speed and isolation.
    This override happens AFTER .env is loaded, so it wins over any
    Redis configuration in .env.
  * Each test gets a fresh in-memory store, so no cross-test leakage.
  * Auth is always configured with a known key.
"""

from __future__ import annotations

import os

# --- Environment setup (before any app import) -------------------------------
# These must be set before importing app.* so that app.config picks them up.
# NOTE: this is not enough on its own — app.config also loads .env via
# python-dotenv, which can override these. We fix that below.
os.environ["ATC_API_KEYS"] = "test-key-1,test-key-2"
os.environ["ATC_ENV"] = "development"
os.environ["ATC_LOG_LEVEL"] = "WARNING"
os.environ.pop("ATC_REDIS_URL", None)

# --- Force in-memory backend at the Settings level ---------------------------
# app.config loads .env on import, which may set ATC_REDIS_URL. We import
# the config module, then forcibly clear redis_url on the frozen settings
# instance. This is the only reliable way to override a value that dotenv
# has already loaded.
import app.config as _config_module  # noqa: E402

object.__setattr__(_config_module.settings, "redis_url", None)

# --- Now safe to import the app ----------------------------------------------
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import session_store  # noqa: E402

# --- Constants ---------------------------------------------------------------

VALID_API_KEY = "test-key-1"
SECOND_API_KEY = "test-key-2"
INVALID_API_KEY = "not-a-real-key"


# --- Fixtures ----------------------------------------------------------------


def _clear_store() -> None:
    """Clear the in-memory session store.

    Safe no-op if the backend is not InMemoryBackend (e.g. if someone
    runs tests against Redis). In that case, tests must use fresh
    session IDs to avoid leakage.
    """
    sessions = getattr(session_store, "_sessions", None)
    lock = getattr(session_store, "_lock", None)
    if sessions is None or lock is None:
        return
    with lock:
        sessions.clear()


@pytest.fixture(autouse=True)
def _reset_sessions():
    """Clear sessions before and after every test (autouse)."""
    _clear_store()
    yield
    _clear_store()


@pytest.fixture()
def client():
    """A FastAPI TestClient bound to the app."""
    with TestClient(app) as c:
        yield c


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