"""Tests for the agent-based pricing model.

This module covers:
  * Agent counting per key (deduplicated, per-month)
  * Agent limit enforcement via /v1/route (402 Payment Required)
  * Response schema: /v1/usage includes agent fields

Coverage gap (honest):
  In the test suite, VALID_API_KEY / SECOND_API_KEY are env-var keys with
  no user_id, so they map to separate `key_<fp>` buckets, NOT a shared
  user bucket. The "agents shared across keys of the same user" loophole
  is therefore not tested here. To test it, an integration test would
  need to register a real user via /v1/auth/register and generate a
  second key via /v1/auth/keys. That test is not written yet.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.usage import get_agents_set, record_agents
from tests.conftest import VALID_API_KEY


# ---------------------------------------------------------------------------
# Fixture: temporarily lower agent limits
# ---------------------------------------------------------------------------


@pytest.fixture()
def small_agent_limit():
    """Temporarily lower team-tier and free-tier agent limits to 3.

    The env-var key VALID_API_KEY resolves to `team` tier, so
    tier_team_agents is the one that matters. We override both for safety.
    The LRU caches are cleared before and after so the new limits take
    effect immediately.
    """
    original_team = settings.tier_team_agents
    original_free = settings.tier_free_agents
    object.__setattr__(settings, "tier_team_agents", 3)
    object.__setattr__(settings, "tier_free_agents", 3)
    from app import usage as _u
    _u._resolve_usage_key.cache_clear()
    _u.get_tier_and_limit.cache_clear()
    yield
    object.__setattr__(settings, "tier_team_agents", original_team)
    object.__setattr__(settings, "tier_free_agents", original_free)
    _u._resolve_usage_key.cache_clear()
    _u.get_tier_and_limit.cache_clear()


# ---------------------------------------------------------------------------
# Direct API: record_agents / get_agents_set
# ---------------------------------------------------------------------------


class TestAgentTracking:
    def test_starts_empty(self):
        assert get_agents_set("brand-new-fp") == set()

    def test_single_handoff_adds_two_agents(self):
        record_agents("fp-a", "coder", "reviewer")
        assert get_agents_set("fp-a") == {"coder", "reviewer"}

    def test_reverse_handoff_deduplicates(self):
        record_agents("fp-b", "coder", "reviewer")
        record_agents("fp-b", "reviewer", "coder")
        agents = get_agents_set("fp-b")
        assert agents == {"coder", "reviewer"}
        assert len(agents) == 2

    def test_third_agent_adds_one(self):
        record_agents("fp-c", "coder", "reviewer")
        record_agents("fp-c", "planner", "coder")
        agents = get_agents_set("fp-c")
        assert agents == {"coder", "reviewer", "planner"}
        assert len(agents) == 3


# ---------------------------------------------------------------------------
# HTTP: agent limit enforcement via /v1/route
# ---------------------------------------------------------------------------


class TestAgentLimitHTTP:
    def test_exceeding_limit_returns_402(
        self, client, auth_headers, sample_message, small_agent_limit
    ):
        """Team tier overridden to 3 agents. Adding a 4th unique agent → 402."""
        # First handoff: coder → reviewer (2 agents)
        msg = dict(sample_message, session_id="s1")
        r = client.post("/v1/route", json=msg, headers=auth_headers)
        assert r.status_code == 200

        # Second handoff: coder → planner (3rd unique agent)
        msg2 = dict(
            sample_message,
            session_id="s2",
            sender="coder",
            recipient="planner",
            payload={"x": 2},
        )
        r = client.post("/v1/route", json=msg2, headers=auth_headers)
        assert r.status_code == 200

        # Third handoff: coder → deployer (would be 4th unique agent → 402)
        msg3 = dict(
            sample_message,
            session_id="s3",
            sender="coder",
            recipient="deployer",
            payload={"x": 3},
        )
        r = client.post("/v1/route", json=msg3, headers=auth_headers)
        assert r.status_code == 402
        body = r.json()
        assert body["error"] == "AGENT_LIMIT_EXCEEDED"
        assert body["session_id"] == "s3"
        diag = body["diagnostics"]
        assert diag["tier"] == "team"
        assert diag["current_agents"] == 3
        assert diag["agent_limit"] == 3
        assert diag["attempted_new_agents"] == ["deployer"]

    def test_existing_agents_still_work_at_limit(
        self, client, auth_headers, sample_message, small_agent_limit
    ):
        """Once at 3/3 agents, handoffs between existing agents are still allowed."""
        # Register 3 agents (coder, reviewer, planner)
        r1 = client.post(
            "/v1/route",
            json=dict(sample_message, session_id="s1"),
            headers=auth_headers,
        )
        assert r1.status_code == 200

        r2 = client.post(
            "/v1/route",
            json=dict(
                sample_message,
                session_id="s2",
                sender="coder",
                recipient="planner",
                payload={"x": 2},
            ),
            headers=auth_headers,
        )
        assert r2.status_code == 200

        # At 3/3. Handoff between two existing agents introduces no new
        # agents, so it must be allowed.
        r3 = client.post(
            "/v1/route",
            json=dict(
                sample_message,
                session_id="s3",
                sender="reviewer",
                recipient="planner",
                payload={"x": 3},
            ),
            headers=auth_headers,
        )
        assert r3.status_code == 200

    def test_rejection_does_not_increment_counters(
        self, client, auth_headers, sample_message, small_agent_limit
    ):
        """A rejected 402 must not increase the message or agent count."""
        # Fill up to 3/3 agents (2 messages)
        client.post(
            "/v1/route",
            json=dict(sample_message, session_id="s1"),
            headers=auth_headers,
        )
        client.post(
            "/v1/route",
            json=dict(
                sample_message,
                session_id="s2",
                sender="coder",
                recipient="planner",
                payload={"x": 2},
            ),
            headers=auth_headers,
        )

        # Attempt to add a 4th agent → 402
        r = client.post(
            "/v1/route",
            json=dict(
                sample_message,
                session_id="s3",
                sender="coder",
                recipient="deployer",
                payload={"x": 3},
            ),
            headers=auth_headers,
        )
        assert r.status_code == 402

        # Snapshot should still show 3 agents / 2 messages
        usage = client.get("/v1/usage", headers=auth_headers).json()
        assert usage["agents_this_month"] == 3
        assert usage["messages_this_month"] == 2


# ---------------------------------------------------------------------------
# Deduplication within a key
# ---------------------------------------------------------------------------


class TestAgentDeduplication:
    def test_agent_count_deduplicates_within_key(
        self, client, auth_headers, sample_message
    ):
        """Many handoffs between the same 2 agents keeps count at 2.

        NOTE: this tests deduplication within a single key, NOT the
        cross-key loophole. test-key-1 has no user_id in the DB, so it
        resolves to `key_<fp>` and shares no bucket with test-key-2.
        """
        for i in range(5):
            client.post(
                "/v1/route",
                json=dict(
                    sample_message,
                    session_id=f"session-{i}",
                    payload={"i": i},
                ),
                headers=auth_headers,
            )

        usage = client.get("/v1/usage", headers=auth_headers).json()
        assert usage["agents_this_month"] == 2


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class TestUsageSchemaWithAgents:
    def test_usage_endpoint_includes_agent_fields(self, client, auth_headers):
        r = client.get("/v1/usage", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert "agents_this_month" in body
        assert "agent_limit" in body
        assert body["agents_this_month"] == 0
        assert body["agent_limit"] == settings.tier_team_agents

    def test_usage_reflects_agent_count(self, client, auth_headers, sample_message):
        client.post("/v1/route", json=sample_message, headers=auth_headers)
        usage = client.get("/v1/usage", headers=auth_headers).json()
        assert usage["agents_this_month"] == 2