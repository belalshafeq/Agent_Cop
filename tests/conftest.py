"""Shared pytest fixtures for the AgentTrafficCop test suite."""

from __future__ import annotations

import os

# --- Environment setup (before any app import) -------------------------------
os.environ["ATC_API_KEYS"] = "test-key-1,test-key-2"
os.environ["ATC_ENV"] = "development"
os.environ["ATC_LOG_LEVEL"] = "WARNING"
os.environ.pop("ATC_REDIS_URL", None)
os.environ.pop("ATC_AUDIT_LOG_ENABLED", None)
os.environ.pop("ATC_AUDIT_LOG_PATH", None)

# --- Force in-memory backend at the Settings level ---------------------------
import app.config as _config_module  # noqa: E402

object.__setattr__(_config_module.settings, "redis_url", None)

# --- Now safe to import the app ----------------------------------------------
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import metrics as _metrics_module  # noqa: E402
from app import usage as _usage_module  # noqa: E402
from app.main import app  # noqa: E402
from app.state import session_store  # noqa: E402

# --- Constants ---------------------------------------------------------------

VALID_API_KEY = "test-key-1"
SECOND_API_KEY = "test-key-2"
INVALID_API_KEY = "not-a-real-key"


# --- Reset helpers -----------------------------------------------------------


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


def _clear_rate_limiter() -> None:
    try:
        import app.middleware as mw
        limiter = getattr(mw, "_limiter", None)
        if limiter is None:
            return
        calls = getattr(limiter, "_calls", None)
        if calls is None:
            return
        calls.clear()
    except Exception:
        pass


def _clear_usage() -> None:
    """Clear the usage counter backend (in-memory variant)."""
    try:
        backend = _usage_module.usage_backend
        data = getattr(backend, "_data", None)
        lock = getattr(backend, "_lock", None)
        if data is not None and lock is not None:
            with lock:
                data.clear()
    except Exception:
        pass


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset sessions, metrics, rate limiter, and usage before/after each test."""
    _clear_store()
    _clear_metrics()
    _clear_rate_limiter()
    _clear_usage()
    yield
    _clear_store()
    _clear_metrics()
    _clear_rate_limiter()
    _clear_usage()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers():
    return {"authorization": f"Bearer {VALID_API_KEY}"}


@pytest.fixture()
def second_auth_headers():
    return {"authorization": f"Bearer {SECOND_API_KEY}"}


@pytest.fixture()
def sample_message():
    return {
        "session_id": "test-session",
        "sender": "coder",
        "recipient": "reviewer",
        "intent": "handoff",
        "payload": {"diff": "fix typo", "file": "app.py"},
    }