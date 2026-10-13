"""Tests for session TTL in the InMemoryBackend.

We construct backends with short TTLs directly (rather than through
the app), so we can test expiry behavior in milliseconds without
relying on the configured production TTL.
"""

from __future__ import annotations

import time

import pytest

from app.state_backends import InMemoryBackend


# ---------------------------------------------------------------------------
# TTL disabled (default)
# ---------------------------------------------------------------------------


class TestTTLDisabled:
    def test_sessions_never_expire_when_ttl_is_zero(self):
        b = InMemoryBackend(ttl_seconds=0)
        b.record("s1", "hash-a")
        time.sleep(0.05)
        snap = b.get("s1")
        assert snap is not None
        assert snap.step == 1

    def test_none_ttl_uses_settings_default(self):
        """When ttl_seconds=None, backend falls back to settings."""
        # settings.session_ttl_seconds defaults to 0 in tests.
        b = InMemoryBackend()
        assert b._ttl == 0


# ---------------------------------------------------------------------------
# TTL enabled
# ---------------------------------------------------------------------------


class TestTTLEnabled:
    def test_session_expires_after_ttl(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("s1", "hash-a")
        assert b.get("s1") is not None

        # Wait past the TTL.
        time.sleep(1.1)
        assert b.get("s1") is None

    def test_expired_session_get_or_create_returns_fresh(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("s1", "hash-a")
        b.record("s1", "hash-b")
        assert b.get("s1").step == 2

        time.sleep(1.1)

        # get_or_create should return a fresh session with step=0
        fresh = b.get_or_create("s1")
        assert fresh.step == 0
        assert fresh.hashes == []

    def test_recording_after_expiry_starts_fresh(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("s1", "hash-a")
        time.sleep(1.1)

        # record() should purge the expired session and start a new one.
        snap = b.record("s1", "hash-b")
        assert snap.step == 1
        assert snap.hashes == ["hash-b"]

    def test_activity_refreshes_ttl(self):
        """A session that keeps being touched doesn't expire."""
        b = InMemoryBackend(ttl_seconds=1)
        for _ in range(5):
            b.record("s1", "hash")
            time.sleep(0.3)
        # ~1.5 seconds elapsed total, but never 1s of idle time.
        assert b.get("s1") is not None
        assert b.get("s1").step == 5

    def test_get_returns_none_for_unknown_session(self):
        b = InMemoryBackend(ttl_seconds=1)
        assert b.get("never-existed") is None

    def test_expiry_does_not_affect_other_sessions(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("fresh", "hash-a")
        b.record("stale", "hash-b")

        time.sleep(0.6)
        # Touch "fresh" so its TTL resets.
        b.record("fresh", "hash-c")

        time.sleep(0.6)
        # Now "stale" is > 1s idle; "fresh" is < 1s.
        assert b.get("stale") is None
        assert b.get("fresh") is not None
        assert b.get("fresh").step == 2


# ---------------------------------------------------------------------------
# list_sessions behavior with TTL
# ---------------------------------------------------------------------------


class TestListSessionsWithTTL:
    def test_list_filters_expired_sessions(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("s1", "hash-a")
        b.record("s2", "hash-b")

        time.sleep(1.1)

        sessions = b.list_sessions()
        assert sessions == []

    def test_list_includes_active_sessions(self):
        b = InMemoryBackend(ttl_seconds=60)
        b.record("s1", "hash-a")
        b.record("s2", "hash-b")

        sessions = b.list_sessions()
        ids = {s.session_id for s in sessions}
        assert ids == {"s1", "s2"}

    def test_list_excludes_only_expired_sessions(self):
        b = InMemoryBackend(ttl_seconds=1)
        b.record("stale", "hash-a")
        time.sleep(0.6)
        b.record("fresh", "hash-b")
        time.sleep(0.6)

        sessions = b.list_sessions()
        ids = {s.session_id for s in sessions}
        # "stale" is now > 1s idle; "fresh" is < 1s.
        assert ids == {"fresh"}


# ---------------------------------------------------------------------------
# active_count behavior with TTL
# ---------------------------------------------------------------------------


class TestActiveCountWithTTL:
    def test_active_count_includes_expired_but_unpurged(self):
        """active_count does NOT filter expired sessions.

        This is a deliberate design decision. Filtering would require
        iterating all sessions on every health check. Lazy eviction
        means the count may include dead sessions until they're touched.
        """
        b = InMemoryBackend(ttl_seconds=1)
        b.record("s1", "hash-a")
        time.sleep(1.1)

        # Still 1 because the record is in the dict, just expired.
        assert b.active_count() == 1

        # But get() will return None.
        assert b.get("s1") is None

        # After the get, the record is purged.
        assert b.active_count() == 0