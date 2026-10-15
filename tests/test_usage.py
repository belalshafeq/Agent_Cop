"""Tests for per-API-key usage metering and tier enforcement."""

from __future__ import annotations

import re

import pytest

from app import usage as usage_module
from app.config import settings
from app.usage import (
    InMemoryUsageBackend,
    current_month_bucket,
    get_tier_and_limit,
    get_usage,
    is_over_limit,
    next_month_start_utc,
    record_message,
    snapshot,
)
from tests.conftest import SECOND_API_KEY, VALID_API_KEY


# ---------------------------------------------------------------------------
# Setting override helper (frozen dataclass)
# ---------------------------------------------------------------------------


@pytest.fixture()
def override_settings():
    """Temporarily override frozen Settings fields.

    Usage:
        def test_x(override_settings):
            override_settings(usage_enforcement_enabled=True, tier_team_limit=3)
    """
    original: dict = {}

    def _override(**kwargs):
        for key, value in kwargs.items():
            if key not in original:
                original[key] = getattr(settings, key)
            object.__setattr__(settings, key, value)

    yield _override

    for key, value in original.items():
        object.__setattr__(settings, key, value)


# ---------------------------------------------------------------------------
# Month bucket helpers
# ---------------------------------------------------------------------------


class TestMonthBuckets:
    def test_current_month_bucket_format(self):
        b = current_month_bucket()
        assert re.fullmatch(r"\d{4}-\d{2}", b), f"unexpected bucket: {b!r}"

    def test_next_month_start_format(self):
        s = next_month_start_utc()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s), s

    def test_next_month_is_in_the_future(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        next_start = datetime.strptime(
            next_month_start_utc(), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        assert next_start > now


# ---------------------------------------------------------------------------
# InMemoryUsageBackend
# ---------------------------------------------------------------------------


class TestInMemoryBackend:
    def test_starts_at_zero(self):
        b = InMemoryUsageBackend()
        assert b.get("unknown") == 0

    def test_increment_returns_new_value(self):
        b = InMemoryUsageBackend()
        assert b.increment("k1") == 1
        assert b.increment("k1") == 2
        assert b.increment("k1", amount=5) == 7
        assert b.get("k1") == 7

    def test_fingerprints_are_independent(self):
        b = InMemoryUsageBackend()
        b.increment("k1")
        b.increment("k1")
        b.increment("k2")
        assert b.get("k1") == 2
        assert b.get("k2") == 1

    def test_thread_safe_increments(self):
        import threading

        b = InMemoryUsageBackend()

        def worker():
            for _ in range(100):
                b.increment("shared")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert b.get("shared") == 500


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------


class TestModuleLevelAPI:
    def test_record_message_increments(self):
        assert get_usage("fp1") == 0
        assert record_message("fp1") == 1
        assert record_message("fp1") == 2
        assert get_usage("fp1") == 2

    def test_snapshot_shape(self):
        record_message("fp2")
        record_message("fp2")
        s = snapshot("fp2")
        d = s.to_dict()
        assert d["api_key_fingerprint"] == "fp2"
        assert d["messages_this_month"] == 2
        assert d["tier"] in {"free", "pro", "team", "enterprise"}
        assert isinstance(d["tier_limit"], int)
        assert d["reset_at"]
        assert isinstance(d["enforcement_enabled"], bool)
        assert d["remaining"] is None or isinstance(d["remaining"], int)


# ---------------------------------------------------------------------------
# Tier lookup
# ---------------------------------------------------------------------------


class TestTierLookup:
    def test_legacy_api_key_resolves_to_team(self):
        from hashlib import sha256

        fp = sha256(VALID_API_KEY.encode()).hexdigest()[:8]
        tier, limit = get_tier_and_limit(fp)
        assert tier == "team"
        assert limit == settings.tier_team_limit

    def test_unknown_fingerprint_defaults_to_free(self):
        tier, limit = get_tier_and_limit("00000000")
        assert tier == "free"
        assert limit == settings.tier_free_limit

    def test_both_legacy_keys_resolve(self):
        from hashlib import sha256

        for key in (VALID_API_KEY, SECOND_API_KEY):
            fp = sha256(key.encode()).hexdigest()[:8]
            tier, _ = get_tier_and_limit(fp)
            assert tier == "team"


# ---------------------------------------------------------------------------
# Limit enforcement
# ---------------------------------------------------------------------------


class TestEnforcement:
    def test_not_over_when_under_limit(self, override_settings):
        override_settings(usage_enforcement_enabled=True, tier_team_limit=10)

        from hashlib import sha256

        fp = sha256(VALID_API_KEY.encode()).hexdigest()[:8]
        for _ in range(5):
            record_message(fp)

        over, tier, used, limit = is_over_limit(fp)
        assert over is False
        assert used == 5
        assert limit == 10

    def test_over_when_at_limit(self, override_settings):
        override_settings(usage_enforcement_enabled=True, tier_team_limit=3)

        from hashlib import sha256

        fp = sha256(VALID_API_KEY.encode()).hexdigest()[:8]
        for _ in range(3):
            record_message(fp)

        over, tier, used, limit = is_over_limit(fp)
        assert over is True
        assert used == 3
        assert limit == 3

    def test_unlimited_never_over(self, override_settings):
        override_settings(usage_enforcement_enabled=True, tier_team_limit=0)

        from hashlib import sha256

        fp = sha256(VALID_API_KEY.encode()).hexdigest()[:8]
        for _ in range(100):
            record_message(fp)

        over, tier, used, limit = is_over_limit(fp)
        assert over is False
        assert limit == 0

    def test_disabled_enforcement_never_over(self, override_settings):
        override_settings(usage_enforcement_enabled=False, tier_team_limit=2)

        from hashlib import sha256

        fp = sha256(VALID_API_KEY.encode()).hexdigest()[:8]
        for _ in range(10):
            record_message(fp)

        over, _, _, _ = is_over_limit(fp)
        assert over is False


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


class TestUsageEndpoint:
    def test_returns_200(self, client, auth_headers):
        r = client.get("/v1/usage", headers=auth_headers)
        assert r.status_code == 200

    def test_requires_auth(self, client):
        r = client.get("/v1/usage")
        assert r.status_code == 401

    def test_shape(self, client, auth_headers):
        r = client.get("/v1/usage", headers=auth_headers)
        body = r.json()
        assert set(body.keys()) == {
            "api_key_fingerprint",
            "tier",
            "messages_this_month",
            "tier_limit",
            "remaining",
            "reset_at",
            "enforcement_enabled",
        }

    def test_counts_increment_through_route(
        self, client, auth_headers, sample_message
    ):
        for i in range(3):
            msg = dict(sample_message, payload={"i": i})
            r = client.post("/v1/route", json=msg, headers=auth_headers)
            assert r.status_code == 200

        usage = client.get("/v1/usage", headers=auth_headers).json()
        assert usage["messages_this_month"] == 3

    def test_rejected_messages_do_not_count(
        self, client, auth_headers, sample_message
    ):
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200

        bad = dict(
            sample_message,
            session_id="bad",
            recipient=sample_message["sender"],
        )
        r = client.post("/v1/route", json=bad, headers=auth_headers)
        assert r.status_code == 422

        usage = client.get("/v1/usage", headers=auth_headers).json()
        assert usage["messages_this_month"] == 1

    def test_two_keys_have_independent_counters(
        self, client, auth_headers, second_auth_headers, sample_message
    ):
        for i in range(2):
            msg = dict(sample_message, session_id=f"k1-{i}", payload={"i": i})
            client.post("/v1/route", json=msg, headers=auth_headers)

        for i in range(4):
            msg = dict(sample_message, session_id=f"k2-{i}", payload={"i": i})
            client.post("/v1/route", json=msg, headers=second_auth_headers)

        u1 = client.get("/v1/usage", headers=auth_headers).json()
        u2 = client.get("/v1/usage", headers=second_auth_headers).json()

        assert u1["messages_this_month"] == 2
        assert u2["messages_this_month"] == 4
        assert u1["api_key_fingerprint"] != u2["api_key_fingerprint"]

    def test_bad_auth_on_usage_endpoint(self, client):
        r = client.get(
            "/v1/usage", headers={"authorization": "Bearer bad-key"}
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Tier limit enforcement via HTTP
# ---------------------------------------------------------------------------


class TestTierEnforcementHTTP:
    def test_enforcement_rejects_when_over_limit(
        self, client, auth_headers, sample_message, override_settings
    ):
        override_settings(
            usage_enforcement_enabled=True,
            tier_team_limit=2,
        )

        for i in range(2):
            msg = dict(sample_message, session_id=f"limit-{i}", payload={"i": i})
            r = client.post("/v1/route", json=msg, headers=auth_headers)
            assert r.status_code == 200, f"unexpected {r.status_code}: {r.text}"

        msg = dict(sample_message, session_id="limit-over", payload={"i": 99})
        r = client.post("/v1/route", json=msg, headers=auth_headers)
        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "TIER_LIMIT_EXCEEDED"
        assert body["diagnostics"]["tier"] == "team"
        assert body["diagnostics"]["limit"] == 2
        assert body["diagnostics"]["used"] == 2
        assert "reset_at" in body["diagnostics"]
        assert "Retry-After" in r.headers

    def test_enforcement_disabled_does_not_reject(
        self, client, auth_headers, sample_message, override_settings
    ):
        override_settings(
            usage_enforcement_enabled=False,
            tier_team_limit=1,
        )

        for i in range(5):
            msg = dict(
                sample_message, session_id=f"noenforce-{i}", payload={"i": i}
            )
            r = client.post("/v1/route", json=msg, headers=auth_headers)
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Usage backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_defaults_to_in_memory(self):
        assert isinstance(
            usage_module.usage_backend, InMemoryUsageBackend
        )