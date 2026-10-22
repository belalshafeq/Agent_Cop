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
    """Clear ALL tier-based rate limiters.

    The middleware now uses a dict of limiters (_limiters) keyed by tier,
    not a single global _limiter. Clearing only _limiter (the old name)
    would silently do nothing and rate-limit state would leak across tests.
    """
    try:
        import app.middleware as mw
        limiters = getattr(mw, "_limiters", None)
        if limiters is None:
            return
        for limiter in limiters.values():
            calls = getattr(limiter, "_calls", None)
            if calls is not None:
                calls.clear()
    except Exception:
        pass


def _clear_usage() -> None:
    """Clear BOTH the message counter and the agent identity set.

    The in-memory backend tracks two dicts:
      - _data: message counts per (resolved_key, month)
      - _agents: agent identity sets per (resolved_key, month)
    Both must be cleared, otherwise agent counts leak across tests
    and the agent-limit tests will fail on the second run.
    """
    try:
        backend = _usage_module.usage_backend
        lock = getattr(backend, "_lock", None)
        data = getattr(backend, "_data", None)
        agents = getattr(backend, "_agents", None)
        if lock is None:
            return
        with lock:
            if data is not None:
                data.clear()
            if agents is not None:
                agents.clear()
    except Exception:
        pass


def _clear_usage_caches() -> None:
    """Clear the @lru_cache wrappers on usage.py lookups.

    Without this, the first test that calls get_tier_and_limit() or
    _resolve_usage_key() caches its result. Any subsequent test that
    overrides settings (e.g. tier_team_limit or tier_team_agents) will
    still see the cached value and the override appears to do nothing.
    This is the classic lru_cache test-pollution trap.
    """
    try:
        _usage_module._resolve_usage_key.cache_clear()
        _usage_module.get_tier_and_limit.cache_clear()
    except Exception:
        pass


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset all mutable global state before and after every test."""
    _clear_store()
    _clear_metrics()
    _clear_rate_limiter()
    _clear_usage()
    _clear_usage_caches()
    yield
    _clear_store()
    _clear_metrics()
    _clear_rate_limiter()
    _clear_usage()
    _clear_usage_caches()


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