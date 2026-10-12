"""HTTP layer tests for AgentTrafficCop's three endpoints.

These tests exercise the full app — routing, auth, Pydantic validation,
circuit breaker, exception handlers — through FastAPI's TestClient.
They are the closest thing to "real" integration tests without a server.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import INVALID_API_KEY, VALID_API_KEY


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_shape(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "AgentTrafficCop"
        assert "version" in body
        assert "active_sessions" in body
        assert "uptime_seconds" in body

    def test_health_does_not_require_auth(self, client):
        # No Authorization header → still 200.
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_active_sessions_starts_at_zero(self, client):
        body = client.get("/health").json()
        assert body["active_sessions"] == 0


# ---------------------------------------------------------------------------
# POST /v1/route — success path
# ---------------------------------------------------------------------------


class TestRouteSuccess:
    def test_approves_valid_message(self, client, auth_headers, sample_message):
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200

        body = r.json()
        assert body["approved"] is True
        assert body["session_id"] == sample_message["session_id"]
        assert body["step"] == 1
        assert len(body["payload_hash"]) == 64

    def test_second_message_increments_step(self, client, auth_headers, sample_message):
        r1 = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r1.json()["step"] == 1

        # Same session, different payload to avoid repeat trip.
        msg2 = dict(sample_message)
        msg2["payload"] = {"diff": "other change", "file": "b.py"}
        r2 = client.post("/v1/route", json=msg2, headers=auth_headers)
        assert r2.json()["step"] == 2

    def test_different_sessions_do_not_interfere(self, client, auth_headers, sample_message):
        msg_a = dict(sample_message)
        msg_a["session_id"] = "session-a"

        msg_b = dict(sample_message)
        msg_b["session_id"] = "session-b"

        client.post("/v1/route", json=msg_a, headers=auth_headers)
        r = client.post("/v1/route", json=msg_b, headers=auth_headers)
        assert r.json()["session_id"] == "session-b"
        assert r.json()["step"] == 1  # its own counter

    def test_health_reflects_session_count(self, client, auth_headers, sample_message):
        client.post("/v1/route", json=sample_message, headers=auth_headers)
        health = client.get("/health").json()
        assert health["active_sessions"] == 1


# ---------------------------------------------------------------------------
# POST /v1/route — auth failures
# ---------------------------------------------------------------------------


class TestRouteAuth:
    def test_missing_authorization_returns_401(self, client, sample_message):
        r = client.post("/v1/route", json=sample_message)
        assert r.status_code == 401
        assert r.json()["detail"]["error"] in {"AUTH_MISSING", "AUTH_NOT_CONFIGURED"}

    def test_invalid_key_returns_401(self, client, sample_message):
        headers = {"authorization": f"Bearer {INVALID_API_KEY}"}
        r = client.post("/v1/route", json=sample_message, headers=headers)
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "AUTH_INVALID"

    def test_malformed_header_returns_401(self, client, sample_message):
        headers = {"authorization": "NotBearer xyz"}
        r = client.post("/v1/route", json=sample_message, headers=headers)
        assert r.status_code == 401

    def test_401_includes_www_authenticate(self, client, sample_message):
        r = client.post("/v1/route", json=sample_message)
        assert "www-authenticate" in {k.lower() for k in r.headers.keys()}


# ---------------------------------------------------------------------------
# POST /v1/route — schema validation
# ---------------------------------------------------------------------------


class TestRouteValidation:
    def test_missing_required_field_returns_422(self, client, auth_headers, sample_message):
        del sample_message["sender"]
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "SCHEMA_VALIDATION_FAILED"
        assert "diagnostics" in body

    def test_empty_payload_returns_422(self, client, auth_headers, sample_message):
        sample_message["payload"] = {}
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422

    def test_unknown_field_returns_422(self, client, auth_headers, sample_message):
        sample_message["rogue"] = "value"
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422

    def test_self_handoff_returns_422(self, client, auth_headers, sample_message):
        sample_message["recipient"] = sample_message["sender"]
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422

    def test_invalid_intent_returns_422(self, client, auth_headers, sample_message):
        sample_message["intent"] = "not-a-real-intent"
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/route — circuit breaker
# ---------------------------------------------------------------------------


class TestRouteCircuitBreaker:
    def test_repeated_payload_trips(self, client, auth_headers, sample_message):
        """Same payload repeatedly → 422 REPEATED_PAYLOAD_LOOP."""
        for _ in range(settings.max_repeated_hashes - 1):
            r = client.post("/v1/route", json=sample_message, headers=auth_headers)
            assert r.status_code == 200

        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "REPEATED_PAYLOAD_LOOP"
        assert body["session_id"] == sample_message["session_id"]
        assert body["diagnostics"]["consecutive_repeats"] == settings.max_repeated_hashes

    def test_cyclic_payload_trips(self, client, auth_headers, sample_message):
        """Alternate two payloads → 422 CYCLIC_PAYLOAD_LOOP."""
        a = dict(sample_message)
        a["payload"] = {"turn": "a"}
        b = dict(sample_message)
        b["payload"] = {"turn": "b"}

        # Send (max_cycle_repeats - 1) full A,B cycles.
        for _ in range(settings.max_cycle_repeats - 1):
            assert client.post("/v1/route", json=a, headers=auth_headers).status_code == 200
            assert client.post("/v1/route", json=b, headers=auth_headers).status_code == 200

        # Send `a` again — approved.
        assert client.post("/v1/route", json=a, headers=auth_headers).status_code == 200

        # Send `b` — completes the cycle → trip.
        r = client.post("/v1/route", json=b, headers=auth_headers)
        assert r.status_code == 422
        assert r.json()["error"] == "CYCLIC_PAYLOAD_LOOP"

    def test_step_limit_trips(self, client, auth_headers, sample_message):
        """Enough distinct payloads → 422 STEP_LIMIT_EXCEEDED."""
        for i in range(settings.max_steps):
            msg = dict(sample_message)
            msg["payload"] = {"i": i}
            r = client.post("/v1/route", json=msg, headers=auth_headers)
            assert r.status_code == 200

        msg = dict(sample_message)
        msg["payload"] = {"i": "overflow"}
        r = client.post("/v1/route", json=msg, headers=auth_headers)
        assert r.status_code == 422
        assert r.json()["error"] == "STEP_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# DELETE /v1/sessions/{id}
# ---------------------------------------------------------------------------


class TestResetSession:
    def test_reset_existing_session(self, client, auth_headers, sample_message):
        # Create a session.
        client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert client.get("/health").json()["active_sessions"] == 1

        # Reset it.
        r = client.delete(
            f"/v1/sessions/{sample_message['session_id']}",
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json() == {"session_id": sample_message["session_id"], "removed": True}

        # Health confirms it's gone.
        assert client.get("/health").json()["active_sessions"] == 0

    def test_reset_unknown_session_returns_removed_false(self, client, auth_headers):
        r = client.delete("/v1/sessions/never-existed", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["removed"] is False

    def test_reset_requires_auth(self, client):
        r = client.delete("/v1/sessions/whatever")
        assert r.status_code == 401

    def test_reset_allows_new_session_lifecycle(self, client, auth_headers, sample_message):
        """After reset, the same session_id can start fresh."""
        for _ in range(settings.max_repeated_hashes - 1):
            client.post("/v1/route", json=sample_message, headers=auth_headers)

        client.delete(
            f"/v1/sessions/{sample_message['session_id']}",
            headers=auth_headers,
        )

        # Same payload — should be approved because the session is fresh.
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["step"] == 1