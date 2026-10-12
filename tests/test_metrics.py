"""Tests for app.metrics and the /metrics endpoint."""

from __future__ import annotations

import re

from app import metrics


# ---------------------------------------------------------------------------
# Collector unit tests
# ---------------------------------------------------------------------------


class TestCounters:
    def test_inc_request_increments(self):
        metrics.inc_request("/v1/route", "POST", 200)
        metrics.inc_request("/v1/route", "POST", 200)
        body = metrics.render()
        assert (
            'atc_requests_total{endpoint="/v1/route",method="POST",status="200"} 2.0'
            in body
        )

    def test_inc_request_different_status_separate_series(self):
        metrics.inc_request("/v1/route", "POST", 200)
        metrics.inc_request("/v1/route", "POST", 422)
        body = metrics.render()
        assert 'status="200"} 1.0' in body
        assert 'status="422"} 1.0' in body

    def test_inc_approved(self):
        metrics.inc_approved()
        metrics.inc_approved()
        body = metrics.render()
        assert "atc_messages_approved_total 2.0" in body

    def test_inc_trip_reason_label(self):
        metrics.inc_trip("repeated_payload")
        metrics.inc_trip("cyclic_payload")
        metrics.inc_trip("repeated_payload")
        body = metrics.render()
        assert 'atc_governance_trips_total{reason="repeated_payload"} 2.0' in body
        assert 'atc_governance_trips_total{reason="cyclic_payload"} 1.0' in body


class TestGauges:
    def test_set_active_sessions(self):
        metrics.set_active_sessions(5)
        body = metrics.render()
        assert "atc_sessions_active 5.0" in body

    def test_gauge_last_value_wins(self):
        metrics.set_active_sessions(5)
        metrics.set_active_sessions(3)
        body = metrics.render()
        assert "atc_sessions_active 3.0" in body
        # The old value should not appear as a duplicate time series.
        # We check the VALUE lines only (lines starting with the metric
        # name, not the "# TYPE ..." comment line).
        value_lines = [
            line for line in body.splitlines()
            if line.startswith("atc_sessions_active ")
        ]
        assert len(value_lines) == 1
        assert value_lines[0] == "atc_sessions_active 3.0"


class TestHistograms:
    def test_observe_latency(self):
        metrics.observe_latency("/v1/route", 0.001)
        metrics.observe_latency("/v1/route", 0.05)
        body = metrics.render()
        assert 'atc_request_duration_seconds_count{endpoint="/v1/route"} 2.0' in body
        assert (
            'atc_request_duration_seconds_bucket{endpoint="/v1/route",le="0.05"} 2.0'
            in body
        )
        assert (
            'atc_request_duration_seconds_bucket{endpoint="/v1/route",le="0.005"} 1.0'
            in body
        )

    def test_inf_bucket_equals_count(self):
        metrics.observe_latency("/v1/route", 0.001)
        metrics.observe_latency("/v1/route", 100.0)
        body = metrics.render()
        assert 'le="+Inf"} 2.0' in body
        assert "count" in body


class TestRenderFormat:
    def test_render_produces_valid_prometheus_format(self):
        metrics.inc_request("/v1/route", "POST", 200)
        metrics.set_active_sessions(1)
        metrics.observe_latency("/v1/route", 0.001)
        body = metrics.render()

        metric_line = re.compile(
            r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? [0-9eE+\-.]+$"
        )
        for line in body.splitlines():
            if not line or line.startswith("#"):
                continue
            assert metric_line.match(line), f"Invalid metric line: {line!r}"

    def test_uptime_is_present(self):
        body = metrics.render()
        assert "atc_uptime_seconds" in body
        assert "# TYPE atc_uptime_seconds gauge" in body

    def test_empty_render_has_uptime_only(self):
        body = metrics.render()
        assert "atc_uptime_seconds" in body
        assert "atc_requests_total" not in body

    def test_label_escaping(self):
        from app.metrics import _escape

        assert _escape('a"b') == 'a\\"b'
        assert _escape("a\\b") == "a\\\\b"
        assert _escape("a\nb") == "a\\nb"


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    def test_endpoint_returns_200(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_endpoint_content_type(self, client):
        r = client.get("/metrics")
        assert "text/plain" in r.headers["content-type"]

    def test_endpoint_contains_expected_metric_names(self, client):
        client.get("/health")
        r = client.get("/metrics")
        body = r.text
        assert "atc_requests_total" in body
        assert "atc_uptime_seconds" in body

    def test_endpoint_records_health_requests(self, client):
        client.get("/health")
        r = client.get("/metrics")
        body = r.text
        assert 'atc_requests_total{endpoint="/health",method="GET",status="200"}' in body


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


class TestMetricsMiddleware:
    def test_successful_route_recorded(self, client, auth_headers, sample_message):
        client.post("/v1/route", json=sample_message, headers=auth_headers)
        r = client.get("/metrics")
        body = r.text
        assert (
            'atc_requests_total{endpoint="/v1/route",method="POST",status="200"} 1.0'
            in body
        )
        assert "atc_messages_approved_total 1.0" in body

    def test_auth_failure_recorded(self, client, sample_message):
        client.post(
            "/v1/route",
            json=sample_message,
            headers={"authorization": "Bearer bad-key"},
        )
        r = client.get("/metrics")
        body = r.text
        assert (
            'atc_requests_total{endpoint="/v1/route",method="POST",status="401"} 1.0'
            in body
        )

    def test_schema_failure_recorded(self, client, auth_headers):
        client.post(
            "/v1/route",
            json={"session_id": "x"},
            headers=auth_headers,
        )
        r = client.get("/metrics")
        body = r.text
        assert (
            'atc_requests_total{endpoint="/v1/route",method="POST",status="422"} 1.0'
            in body
        )

    def test_governance_trip_recorded(self, client, auth_headers):
        from app.config import settings

        payload = {"stuck": True}
        msg = {
            "session_id": "trip-test",
            "sender": "coder",
            "recipient": "reviewer",
            "intent": "handoff",
            "payload": payload,
        }

        for _ in range(settings.max_repeated_hashes):
            client.post("/v1/route", json=msg, headers=auth_headers)

        r = client.get("/metrics")
        body = r.text
        assert 'atc_governance_trips_total{reason="repeated_payload"} 1.0' in body
        assert (
            'atc_requests_total{endpoint="/v1/route",method="POST",status="422"} 1.0'
            in body
        )

    def test_multiple_endpoints_recorded_separately(self, client, auth_headers):
        client.get("/health")
        client.get("/metrics")
        r = client.get("/metrics")
        body = r.text
        assert 'endpoint="/health"' in body
        assert 'endpoint="/metrics"' in body

    def test_metrics_do_not_break_requests(self, client, auth_headers, sample_message):
        r = client.post("/v1/route", json=sample_message, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["approved"] is True