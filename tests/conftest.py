"""Shared pytest fixtures for the AgentTrafficCop test suite."""

from __future__ import annotations

import os

# --- Environment setup (before any app import) -------------------------------
os.environ["ATC_API_KEYS"] = "test-key-1,test-key-2"
os.environ["ATC_ENV"] = "development"
os.environ["ATC_LOG_LEVEL"] = "WARNING"
os.environ.pop("ATC_REDIS_URL", None)
# Disable audit logging by default; individual tests enable it explicitly.
os.environ.pop("ATC_AUDIT_LOG_ENABLED", None)
os.environ.pop("ATC_AUDIT_LOG_PATH", None)

# --- Force in-memory backend at the Settings level ---------------------------
import app.config as _config_module  # noqa: E402

object.__setattr__(_config_module.settings, "redis_url", None)

# --- Now safe to import the app ----------------------------------------------
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import metrics as _metrics_module  # noqa: E402
from app.main import app  # noqa: E402
from app.state import session_store  # noqa: E402

# --- Constants ---------------------------------------------------------------

VALID_API_KEY = "test-key-1"
SECOND_API_KEY = "test-key-2"
INVALID_API_KEY = "not-a-real-key"


# --- Fixtures ----------------------------------------------------------------


def _clear_store() -> None:
    sessions = getattr(session_store, "_sessions", None)
    lock = getattr(session_store, "_lock", None)
    if sessions is None or lock is None:
        return
    with lock:
        sessions.clear()


def _clear_metrics() -> None:
    try:
        _metrics_module._counters._data.clear()
        _metrics_module._gauges._data.clear()
        _metrics_module._histograms._data.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset sessions, metrics, and audit state before/after each test."""
    _clear_store()
    _clear_metrics()
    yield
    _clear_store()
    _clear_metrics()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers():
    return {"authorization": f"Bearer {VALID_API_KEY}"}


@pytest.fixture()
def sample_message():
    return {
        "session_id": "test-session",
        "sender": "coder",
        "recipient": "reviewer",
        "intent": "handoff",
        "payload": {"diff": "fix typo", "file": "app.py"},
    }