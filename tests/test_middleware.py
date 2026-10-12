"""Tests for HTTP middleware: body size enforcement."""

from __future__ import annotations

from app.config import settings


class TestRateLimitConfig:
    def test_rate_limit_setting_exists(self):
        assert isinstance(settings.rate_limit_per_minute, int)
        assert settings.rate_limit_per_minute > 0

    def test_429_declared_in_openapi(self, client):
        """OpenAPI docs should declare the 429 response for /v1/route."""
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json().get("paths", {})
        route = paths.get("/v1/route", {}).get("post", {})
        responses = route.get("responses", {})
        assert "429" in responses


class TestBodySizeLimit:
    def test_normal_request_is_under_limit(self, client, auth_headers, sample_message):
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200

    def test_oversized_request_returns_413(self, client, auth_headers):
        huge = "x" * (2 * settings.max_body_bytes)
        body = {
            "session_id": "big",
            "sender": "coder",
            "recipient": "reviewer",
            "intent": "handoff",
            "payload": {"data": huge},
        }
        r = client.post("/v1/route", json=body, headers=auth_headers)
        assert r.status_code == 413
        body_json = r.json()
        assert body_json["error"] == "REQUEST_TOO_LARGE"
        assert body_json["diagnostics"]["max_bytes"] == settings.max_body_bytes

    def test_413_response_shape(self, client, auth_headers):
        huge = "x" * (2 * settings.max_body_bytes)
        body = {
            "session_id": "big",
            "sender": "coder",
            "recipient": "reviewer",
            "intent": "handoff",
            "payload": {"data": huge},
        }
        r = client.post("/v1/route", json=body, headers=auth_headers)
        assert r.status_code == 413
        body_json = r.json()
        assert set(body_json.keys()) >= {"error", "message", "session_id", "diagnostics"}