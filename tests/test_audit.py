"""Tests for the audit log writer and its integration with the app."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import pytest

from app.audit import AuditLog


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_audit_path():
    """Yield a temporary file path. Deletes the file on teardown."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    tmp.close()
    path = tmp.name
    yield path
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def read_lines(path: str) -> list[dict]:
    """Read all JSON Lines from *path* as a list of dicts."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Writer unit tests
# ---------------------------------------------------------------------------


class TestAuditWriter:
    def test_disabled_is_a_no_op(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=False)
        a.log_trip(session_id="s", error_code="X", step=1)
        a.log_reset(session_id="s", removed=True)
        a.close()
        # File should be empty (or nonexistent data).
        assert Path(tmp_audit_path).read_text() == ""

    def test_no_path_is_a_no_op(self):
        a = AuditLog(path=None, enabled=True)
        assert a.enabled is False
        a.log_trip(session_id="s", error_code="X", step=1)
        a.close()

    def test_log_trip_writes_valid_json(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_trip(
            session_id="s1",
            error_code="REPEATED_PAYLOAD_LOOP",
            step=3,
            sender="coder",
            recipient="reviewer",
            diagnostics={"repeats": 3},
            api_key_fingerprint="abc12345",
        )
        a.close()

        lines = read_lines(tmp_audit_path)
        assert len(lines) == 1
        rec = lines[0]
        assert rec["event"] == "trip"
        assert rec["error_code"] == "REPEATED_PAYLOAD_LOOP"
        assert rec["session_id"] == "s1"
        assert rec["step"] == 3
        assert rec["sender"] == "coder"
        assert rec["recipient"] == "reviewer"
        assert rec["diagnostics"] == {"repeats": 3}
        assert rec["api_key_fingerprint"] == "abc12345"
        assert "ts" in rec

    def test_log_reset(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_reset(session_id="s2", removed=True)
        a.close()
        rec = read_lines(tmp_audit_path)[0]
        assert rec["event"] == "reset"
        assert rec["session_id"] == "s2"
        assert rec["removed"] is True

    def test_log_auth_failed(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_auth_failed(reason="invalid_key", api_key_fingerprint="xyz")
        a.close()
        rec = read_lines(tmp_audit_path)[0]
        assert rec["event"] == "auth_failed"
        assert rec["reason"] == "invalid_key"
        assert rec["api_key_fingerprint"] == "xyz"

    def test_log_schema_failed(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_schema_failed(session_id="s3", error_count=4)
        a.close()
        rec = read_lines(tmp_audit_path)[0]
        assert rec["event"] == "schema_failed"
        assert rec["session_id"] == "s3"
        assert rec["error_count"] == 4

    def test_log_rate_limited(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_rate_limited(path="/v1/route", retry_after=60)
        a.close()
        rec = read_lines(tmp_audit_path)[0]
        assert rec["event"] == "rate_limited"
        assert rec["path"] == "/v1/route"
        assert rec["retry_after_seconds"] == 60

    def test_multiple_events_append(self, tmp_audit_path):
        a = AuditLog(path=tmp_audit_path, enabled=True)
        a.log_trip(session_id="s1", error_code="X", step=1)
        a.log_reset(session_id="s1", removed=True)
        a.log_schema_failed(session_id="s2", error_count=2)
        a.close()
        lines = read_lines(tmp_audit_path)
        assert len(lines) == 3
        assert [r["event"] for r in lines] == ["trip", "reset", "schema_failed"]

    def test_unwritable_path_fails_soft(self):
        # Path in a directory that doesn't exist and can't be created.
        # /proc is a read-only filesystem on Linux in most cases.
        a = AuditLog(path="/proc/cannot_write_here.log", enabled=True)
        # The AuditLog should have disabled itself.
        assert a.enabled is False
        # And calling log methods should not raise.
        a.log_trip(session_id="s", error_code="X", step=1)
        a.close()

    def test_concurrent_writes_do_not_interleave(self, tmp_audit_path):
        """Multiple threads writing at once produce well-formed lines."""
        a = AuditLog(path=tmp_audit_path, enabled=True)

        def writer(n: int) -> None:
            for i in range(20):
                a.log_trip(session_id=f"s{n}", error_code="X", step=i)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        a.close()

        lines = read_lines(tmp_audit_path)
        # 5 threads * 20 writes = 100 lines, all valid JSON.
        assert len(lines) == 100
        # Every line has the same shape.
        for rec in lines:
            assert rec["event"] == "trip"


# ---------------------------------------------------------------------------
# Integration: HTTP layer writes to the audit log
# ---------------------------------------------------------------------------


class TestAuditIntegration:
    """Enable audit logging for these tests, using a temp file."""

    @pytest.fixture()
    def enable_audit(self, tmp_audit_path, monkeypatch):
        """Swap the module-level audit singleton for a temp-file one."""
        import app.audit as audit_module

        new_audit = AuditLog(path=tmp_audit_path, enabled=True)
        monkeypatch.setattr(audit_module, "audit", new_audit)
        # Also rebind the names imported by other modules.
        import app.routes as routes_module
        import app.security as security_module
        import app.exceptions as exceptions_module
        import app.circuit_breaker as breaker_module
        monkeypatch.setattr(routes_module, "audit", new_audit)
        monkeypatch.setattr(security_module, "audit", new_audit)
        monkeypatch.setattr(exceptions_module, "audit", new_audit)
        monkeypatch.setattr(breaker_module, "audit", new_audit)
        yield new_audit
        new_audit.close()

    def test_trip_writes_audit_line(
        self, client, auth_headers, enable_audit, tmp_audit_path
    ):
        """Trip the breaker via HTTP and confirm an audit line lands."""
        from app.config import settings

        payload = {"stuck": True}
        msg = {
            "session_id": "audit-http-trip",
            "sender": "coder",
            "recipient": "reviewer",
            "intent": "handoff",
            "payload": payload,
        }

        for _ in range(settings.max_repeated_hashes):
            client.post("/v1/route", json=msg, headers=auth_headers)

        lines = read_lines(tmp_audit_path)
        trips = [l for l in lines if l["event"] == "trip"]
        assert len(trips) == 1
        assert trips[0]["error_code"] == "REPEATED_PAYLOAD_LOOP"
        assert trips[0]["session_id"] == "audit-http-trip"

    def test_reset_writes_audit_line(
        self, client, auth_headers, enable_audit, tmp_audit_path
    ):
        client.delete("/v1/sessions/some-session", headers=auth_headers)
        lines = read_lines(tmp_audit_path)
        resets = [l for l in lines if l["event"] == "reset"]
        assert len(resets) == 1
        assert resets[0]["session_id"] == "some-session"

    def test_auth_failure_writes_audit_line(
        self, client, enable_audit, tmp_audit_path, sample_message
    ):
        client.post(
            "/v1/route",
            json=sample_message,
            headers={"authorization": "Bearer bad-key"},
        )
        lines = read_lines(tmp_audit_path)
        auth = [l for l in lines if l["event"] == "auth_failed"]
        assert len(auth) == 1
        assert auth[0]["reason"] == "invalid_key"

    def test_schema_failure_writes_audit_line(
        self, client, auth_headers, enable_audit, tmp_audit_path
    ):
        client.post(
            "/v1/route",
            json={"session_id": "x"},
            headers=auth_headers,
        )
        lines = read_lines(tmp_audit_path)
        sf = [l for l in lines if l["event"] == "schema_failed"]
        assert len(sf) == 1
        assert sf[0]["error_count"] >= 1

    def test_successful_route_does_not_write_audit_line(
        self, client, auth_headers, enable_audit, tmp_audit_path, sample_message
    ):
        """Approvals are NOT logged — only trips, resets, and errors."""
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200

        lines = read_lines(tmp_audit_path)
        # Only non-approval events should be present, and there should be none.
        assert lines == []