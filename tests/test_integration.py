"""End-to-end integration test: a two-agent loop through the HTTP layer.

This is the test that simulates the real product scenario:

    Agent A ──▶ AgentTrafficCop ──▶ Agent B
              ◀── (loop until trip) ──

It exercises:
  * Real HTTP requests via TestClient
  * Real auth
  * Real Pydantic validation
  * Real SHA-256 hashing
  * Real circuit breaker
  * Real structured error responses

If this test passes, the product works.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import VALID_API_KEY


# ---------------------------------------------------------------------------
# Fixtures specific to integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def headers():
    return {"authorization": f"Bearer {VALID_API_KEY}"}


def send(client, headers, session_id, sender, recipient, payload):
    """Send one message through the cop. Returns the HTTP response."""
    return client.post(
        "/v1/route",
        headers=headers,
        json={
            "session_id": session_id,
            "sender": sender,
            "recipient": recipient,
            "intent": "handoff",
            "payload": payload,
        },
    )


# ---------------------------------------------------------------------------
# Scenario 1: The clean shutdown
# ---------------------------------------------------------------------------


class TestCleanWorkflow:
    """A short, real conversation that never loops."""

    def test_three_turn_conversation_completes(self, client, headers):
        """Coder → Reviewer → Coder → done. All approved."""
        session = "clean-workflow"

        r1 = send(client, headers, session, "coder", "reviewer", {"draft": "v1"})
        assert r1.status_code == 200
        assert r1.json()["step"] == 1

        r2 = send(client, headers, session, "reviewer", "coder", {"feedback": "add doc"})
        assert r2.status_code == 200
        assert r2.json()["step"] == 2

        r3 = send(client, headers, session, "coder", "reviewer", {"draft": "v2 with doc"})
        assert r3.status_code == 200
        assert r3.json()["step"] == 3

        # Session is healthy.
        assert client.get("/health").json()["active_sessions"] == 1


# ---------------------------------------------------------------------------
# Scenario 2: The infinite loop — the whole reason this product exists
# ---------------------------------------------------------------------------


class TestCoderReviewerLoop:
    """The canonical failure mode: two agents stuck in a loop."""

    def test_repeated_fix_loop_trips(self, client, headers):
        """Coder sends the same fix repeatedly → trip."""
        session = "loop-repeat"

        # The coder is stuck — sends the same payload every time.
        stuck_payload = {"diff": "fix typo in app.py", "file": "app.py"}

        # First few iterations are approved.
        for i in range(settings.max_repeated_hashes - 1):
            r = send(client, headers, session, "coder", "reviewer", stuck_payload)
            assert r.status_code == 200, f"Unexpected rejection at step {i + 1}"

        # The breaking point.
        r = send(client, headers, session, "coder", "reviewer", stuck_payload)
        assert r.status_code == 422

        body = r.json()
        assert body["error"] == "REPEATED_PAYLOAD_LOOP"
        assert body["session_id"] == session
        assert body["diagnostics"]["consecutive_repeats"] == settings.max_repeated_hashes
        assert body["diagnostics"]["step"] == settings.max_repeated_hashes

    def test_ping_pong_loop_trips(self, client, headers):
        """Coder and Reviewer ping-pong forever → trip."""
        session = "loop-pingpong"

        # Each round trip is one complete cycle.
        # We're going to send (max_cycle_repeats - 1) complete cycles first.
        for _ in range(settings.max_cycle_repeats - 1):
            assert send(client, headers, session, "coder", "reviewer",
                        {"diff": "v1"}).status_code == 200
            assert send(client, headers, session, "reviewer", "coder",
                        {"feedback": "change line 42"}).status_code == 200

        # Start the next cycle.
        assert send(client, headers, session, "coder", "reviewer",
                    {"diff": "v1"}).status_code == 200

        # The reviewer's reply completes the 3rd cycle → trip.
        r = send(client, headers, session, "reviewer", "coder",
                 {"feedback": "change line 42"})
        assert r.status_code == 422
        assert r.json()["error"] == "CYCLIC_PAYLOAD_LOOP"
        assert r.json()["diagnostics"]["cycle_length"] == 2

    def test_step_limit_trips_for_infinite_novel_loop(self, client, headers):
        """An agent that keeps producing NEW payloads forever → step limit trip."""
        session = "loop-novel"

        # Every payload is different, so repeat/cycle detection never fires.
        # Only the step limit can save us.
        for i in range(settings.max_steps):
            r = send(client, headers, session, "coder", "reviewer", {"i": i})
            assert r.status_code == 200, f"Unexpected rejection at step {i + 1}"

        # The next message exceeds the limit.
        r = send(client, headers, session, "coder", "reviewer", {"i": "overflow"})
        assert r.status_code == 422
        assert r.json()["error"] == "STEP_LIMIT_EXCEEDED"
        assert r.json()["diagnostics"]["max_steps"] == settings.max_steps


# ---------------------------------------------------------------------------
# Scenario 3: The recovery — operator intervenes
# ---------------------------------------------------------------------------


class TestOperatorRecovery:
    """After a loop trips, an operator can reset and try again."""

    def test_reset_recovers_from_tripped_session(self, client, headers):
        """Trip the breaker, reset the session, then it works again."""
        session = "recover"
        payload = {"diff": "still broken"}

        # Trip the breaker.
        for _ in range(settings.max_repeated_hashes - 1):
            send(client, headers, session, "coder", "reviewer", payload)

        r = send(client, headers, session, "coder", "reviewer", payload)
        assert r.status_code == 422

        # Operator resets the session.
        r = client.delete(f"/v1/sessions/{session}", headers=headers)
        assert r.status_code == 200
        assert r.json()["removed"] is True

        # The same payload is now approved fresh.
        r = send(client, headers, session, "coder", "reviewer", payload)
        assert r.status_code == 200
        assert r.json()["step"] == 1


# ---------------------------------------------------------------------------
# Scenario 4: Multiple independent sessions
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    """Two agents talking in different sessions do not interfere."""

    def test_parallel_sessions_are_independent(self, client, headers):
        """Trip session A; session B keeps working."""
        payload = {"shared": "payload"}

        # Trip session A.
        for _ in range(settings.max_repeated_hashes - 1):
            send(client, headers, "session-A", "coder", "reviewer", payload)
        r = send(client, headers, "session-A", "coder", "reviewer", payload)
        assert r.status_code == 422

        # Session B is unaffected.
        r = send(client, headers, "session-B", "coder", "reviewer", payload)
        assert r.status_code == 200
        assert r.json()["session_id"] == "session-B"
        assert r.json()["step"] == 1

        # Health reports both sessions.
        assert client.get("/health").json()["active_sessions"] == 2


# ---------------------------------------------------------------------------
# Scenario 5: The full story — a loop with a specific cost ceiling
# ---------------------------------------------------------------------------


class TestBudgetProtection:
    """Simulate the money-saving scenario from the README."""

    def test_breaker_saves_steps(self, client, headers):
        """Without the breaker, an infinite loop runs forever.
        With it, we stop at step N. Verify the N is small."""
        session = "budget"

        # A stuck loop trips at max_repeated_hashes.
        payload = {"stuck": True}
        approved_steps = 0

        # Send a lot of messages; only a small number should be approved.
        for _ in range(50):
            r = send(client, headers, session, "coder", "reviewer", payload)
            if r.status_code == 200:
                approved_steps += 1
            else:
                break

        # Approvals stop at (max_repeated_hashes - 1).
        # The Nth identical message trips and is NOT counted as approved.
        assert approved_steps == settings.max_repeated_hashes - 1
        # Not 50.
        assert approved_steps < 50