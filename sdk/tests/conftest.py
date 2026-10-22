"""Shared fixtures for the AgentTrafficCop SDK test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Add repo root and SDK root to sys.path BEFORE importing app.* -----------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SDK_ROOT = _REPO_ROOT / "sdk"
for p in (str(_REPO_ROOT), str(_SDK_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# --- Environment setup (same keys as main tests) -----------------------------
os.environ["ATC_API_KEYS"] = "test-key-1,test-key-2"
os.environ["ATC_ENV"] = "development"
os.environ["ATC_LOG_LEVEL"] = "WARNING"
os.environ.pop("ATC_REDIS_URL", None)
os.environ.pop("ATC_AUDIT_LOG_ENABLED", None)
os.environ.pop("ATC_AUDIT_LOG_PATH", None)

# --- Force in-memory backend for the server under test -----------------------
import app.config as _config_module  # noqa: E402

object.__setattr__(_config_module.settings, "redis_url", None)

# --- Imports after env setup -------------------------------------------------
import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

import app.metrics as _metrics_module  # noqa: E402
import app.usage as _usage_module  # noqa: E402
from app.main import app  # noqa: E402
from app.state import session_store  # noqa: E402


VALID_API_KEY = "test-key-1"
SECOND_API_KEY = "test-key-2"
INVALID_API_KEY = "not-a-real-key"
SDK_BASE_URL = "http://testserver"


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
    """Clear ALL tier-based rate limiters (dict _limiters, not _limiter)."""
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
    """Clear the message counter and agent identity set."""
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
    """Clear the @lru_cache wrappers on usage.py lookups."""
    try:
        _usage_module._resolve_usage_key.cache_clear()
        _usage_module.get_tier_and_limit.cache_clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_sdk_state():
    """Reset all mutable global state between SDK tests."""
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


@pytest.fixture(autouse=True)
def _asgi_transport(monkeypatch):
    """Route httpx.AsyncClient through the ASGI app for in-process testing."""
    original_async_client = httpx.AsyncClient
    transport = ASGITransport(app=app)

    def patched_async_client(*args, **kwargs):
        kwargs.pop("base_url", None)
        return original_async_client(transport=transport, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)
    yield


@pytest.fixture()
def sdk_base_url() -> str:
    return SDK_BASE_URL


@pytest.fixture()
def valid_api_key() -> str:
    return VALID_API_KEY


@pytest.fixture()
def invalid_api_key() -> str:
    return INVALID_API_KEY