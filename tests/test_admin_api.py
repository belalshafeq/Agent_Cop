"""Tests for the Admin API endpoints: list, detail, cleanup."""

from __future__ import annotations

import time

import pytest

from app.config import settings
from tests.conftest import VALID_API_KEY


# ---------------------------------------------------------------------------
# GET /v1/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_empty_store_returns_empty_list(self, client, auth_headers):
        r = client.get("/v1/sessions", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["returned"] == 0
        assert body["sessions"] == []

    def test_requires_auth(self, client):
        r = client.get("/v1/sessions")
        assert r.status_code == 401

    def test_lists_created_sessions(
        self, client, auth_headers, sample_message
    ):
        # Create two sessions
        msg_a = dict(sample_message, session_id="list-test-a")
        msg_b = dict(sample_message, session_id="list-test-b")
        client.post("/v1/route", json=msg_a, headers=auth_headers)
        client.post("/v1/route", json=msg_b, headers=auth_headers)

        r = client.get("/v1/sessions", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert body["returned"] == 2
        ids = {s["session_id"] for s in body["sessions"]}
        assert ids == {"list-test-a", "list-test-b"}

    def test_summary_fields_present(self, client, auth_headers, sample_message):
        msg = dict(sample_message, session_id="fields-test")
        client.post("/v1/route", json=msg, headers=auth_headers)

        r = client.get("/v1/sessions", headers=auth_headers)
        s = r.json()["sessions"][0]
        assert s["session_id"] == "fields-test"
        assert s["step"] == 1
        assert s["hash_count"] == 1
        assert isinstance(s["created_at"], float)
        assert isinstance(s["last_seen"], float)
        assert s["age_seconds"] >= 0.0
        assert s["idle_seconds"] >= 0.0

    def test_limit_and_offset(self, client, auth_headers, sample_message):
        for i in range(5):
            msg = dict(sample_message, session_id=f"page-test-{i}")
            client.post("/v1/route", json=msg, headers=auth_headers)

        r = client.get("/v1/sessions?limit=2&offset=0", headers=auth_headers)
        body = r.json()
        assert body["total"] == 5
        assert body["returned"] == 2

        r2 = client.get("/v1/sessions?limit=2&offset=2", headers=auth_headers)
        body2 = r2.json()
        assert body2["returned"] == 2
        assert body2["total"] == 5

    def test_min_step_filter(self, client, auth_headers, sample_message):
        # Session A: 1 step. Session B: 3 steps.
        for i in range(1):
            msg = dict(sample_message, session_id="filter-a", payload={"i": i})
            client.post("/v1/route", json=msg, headers=auth_headers)
        for i in range(3):
            msg = dict(sample_message, session_id="filter-b", payload={"i": i})
            client.post("/v1/route", json=msg, headers=auth_headers)

        r = client.get("/v1/sessions?min_step=2", headers=auth_headers)
        body = r.json()
        ids = {s["session_id"] for s in body["sessions"]}
        assert "filter-b" in ids
        assert "filter-a" not in ids

    def test_ordered_by_last_seen_descending(
        self, client, auth_headers, sample_message
    ):
        msg_a = dict(sample_message, session_id="order-a")
        client.post("/v1/route", json=msg_a, headers=auth_headers)
        time.sleep(0.02)
        msg_b = dict(sample_message, session_id="order-b")
        client.post("/v1/route", json=msg_b, headers=auth_headers)

        r = client.get("/v1/sessions", headers=auth_headers)
        ids = [s["session_id"] for s in r.json()["sessions"]]
        # Most recently used first.
        assert ids[0] == "order-b"
        assert ids[1] == "order-a"


# ---------------------------------------------------------------------------
# GET /v1/sessions/{id}
# ---------------------------------------------------------------------------


class TestSessionDetail:
    def test_requires_auth(self, client):
        r = client.get("/v1/sessions/anything")
        assert r.status_code == 401

    def test_returns_404_for_unknown_session(self, client, auth_headers):
        r = client.get("/v1/sessions/never-existed", headers=auth_headers)
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "SESSION_NOT_FOUND"

    def test_404_does_not_create_phantom_session(self, client, auth_headers):
        """Calling GET for a nonexistent session must not create one."""
        client.get("/v1/sessions/never-existed", headers=auth_headers)
        # Health reflects the true session count.
        health = client.get("/health").json()
        assert health["active_sessions"] == 0

    def test_returns_full_history(self, client, auth_headers, sample_message):
        msg = dict(sample_message, session_id="detail-test")
        client.post("/v1/route", json=msg, headers=auth_headers)
        # Second, distinct payload to add another hash.
        msg2 = dict(sample_message, session_id="detail-test", payload={"i": 2})
        client.post("/v1/route", json=msg2, headers=auth_headers)

        r = client.get("/v1/sessions/detail-test", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "detail-test"
        assert body["step"] == 2
        assert body["hash_count"] == 2
        assert len(body["hashes"]) == 2
        assert all(len(h) == 64 for h in body["hashes"])


# ---------------------------------------------------------------------------
# POST /v1/admin/cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_requires_auth(self, client):
        r = client.post(
            "/v1/admin/cleanup",
            json={"older_than_seconds": 3600},
        )
        assert r.status_code == 401

    def test_validation_rejects_zero(self, client, auth_headers):
        r = client.post(
            "/v1/admin/cleanup",
            json={"older_than_seconds": 0},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_validation_rejects_missing_field(self, client, auth_headers):
        r = client.post(
            "/v1/admin/cleanup",
            json={},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_cleanup_expires_old_sessions(
        self, client, auth_headers, sample_message
    ):
        msg = dict(sample_message, session_id="cleanup-test")
        client.post("/v1/route", json=msg, headers=auth_headers)
        assert client.get("/health").json()["active_sessions"] == 1

        # Wait long enough that the session is older than 1 second.
        time.sleep(1.1)

        r = client.post(
            "/v1/admin/cleanup",
            json={"older_than_seconds": 1},
            headers=auth_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["expired"] == 1
        assert body["remaining"] == 0

        assert client.get("/health").json()["active_sessions"] == 0

    def test_cleanup_leaves_recent_sessions(
        self, client, auth_headers, sample_message
    ):
        msg = dict(sample_message, session_id="cleanup-recent")
        client.post("/v1/route", json=msg, headers=auth_headers)

        # Cleanup with a large threshold — nothing should be touched.
        r = client.post(
            "/v1/admin/cleanup",
            json={"older_than_seconds": 3600},
            headers=auth_headers,
        )
        body = r.json()
        assert body["expired"] == 0
        assert body["remaining"] == 1

    def test_cleanup_writes_audit_entries(
        self, client, auth_headers, sample_message, monkeypatch
    ):
        """Every expired session should produce an audit 'reset' event."""
        import tempfile
        from pathlib import Path
        import json

        from app.audit import AuditLog
        import app.audit as audit_module
        import app.routes as routes_module
        import app.security as security_module
        import app.exceptions as exceptions_module
        import app.circuit_breaker as breaker_module

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
        tmp.close()
        new_audit = AuditLog(path=tmp.name, enabled=True)

        monkeypatch.setattr(audit_module, "audit", new_audit)
        monkeypatch.setattr(routes_module, "audit", new_audit)
        monkeypatch.setattr(security_module, "audit", new_audit)
        monkeypatch.setattr(exceptions_module, "audit", new_audit)
        monkeypatch.setattr(breaker_module, "audit", new_audit)

        try:
            msg = dict(sample_message, session_id="cleanup-audit")
            client.post("/v1/route", json=msg, headers=auth_headers)
            time.sleep(1.1)
            r = client.post(
                "/v1/admin/cleanup",
                json={"older_than_seconds": 1},
                headers=auth_headers,
            )
            assert r.json()["expired"] == 1

            lines = [
                json.loads(l)
                for l in Path(tmp.name).read_text().splitlines()
                if l.strip()
            ]
            resets = [l for l in lines if l["event"] == "reset"]
            assert len(resets) == 1
            assert resets[0]["session_id"] == "cleanup-audit"
        finally:
            new_audit.close()
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except Exception:
                pass