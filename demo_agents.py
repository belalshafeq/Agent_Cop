"""Two toy agents for the AgentTrafficCop demo.

These are NOT real LLM agents. They simulate the *pattern* of two
agents getting stuck in a loop — the Coder keeps "fixing" the same
thing, the Reviewer keeps asking for the same change. The point is to
show the loop in the terminal, not to be clever.

Each agent produces dicts that match the AgentMessage schema:

    {
        "sender":    "coder" | "reviewer",
        "recipient": "reviewer" | "coder",
        "intent":    "handoff",
        "payload":   {...},
    }

The driver (demo.py) is responsible for adding session_id and calling
AgentTrafficCop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Message shape (matches AgentMessage in app/schemas.py)
# ---------------------------------------------------------------------------


@dataclass
class OutgoingMessage:
    sender: str
    recipient: str
    intent: str
    payload: Dict[str, Any]


# ---------------------------------------------------------------------------
# Coder agent
# ---------------------------------------------------------------------------


class Coder:
    """A toy Coder agent. Produces 'fix' payloads.

    After `stuck_after` distinct turns, it starts emitting the SAME
    payload every time — simulating the loop where a coder thinks it
    fixed something and the reviewer disagrees.
    """

    def __init__(self, stuck_after: int = 2) -> None:
        self.turn = 0
        self.stuck_after = stuck_after
        self._real_fixes = [
            {"diff": "rename foo to bar in app.py", "file": "app.py", "line": 12},
            {"diff": "add null check in handle()", "file": "app.py", "line": 42},
        ]

    def next_message(self) -> OutgoingMessage:
        self.turn += 1

        if self.turn <= self.stuck_after:
            # Produce a genuinely new fix.
            payload = self._real_fixes[self.turn - 1]
        else:
            # Got stuck — same payload as the previous turn.
            payload = self._real_fixes[-1]

        return OutgoingMessage(
            sender="coder",
            recipient="reviewer",
            intent="handoff",
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Reviewer agent
# ---------------------------------------------------------------------------


class Reviewer:
    """A toy Reviewer agent. Produces 'feedback' payloads.

    Mirrors the Coder: after `stuck_after` distinct turns, it repeats
    the same feedback every time. Together, Coder + Reviewer form the
    classic A↔B ping-pong loop.
    """

    def __init__(self, stuck_after: int = 2) -> None:
        self.turn = 0
        self.stuck_after = stuck_after
        self._real_feedback = [
            {"feedback": "please add a docstring", "file": "app.py", "line": 12},
            {"feedback": "please handle the empty case", "file": "app.py", "line": 42},
        ]

    def next_message(self) -> OutgoingMessage:
        self.turn += 1

        if self.turn <= self.stuck_after:
            payload = self._real_feedback[self.turn - 1]
        else:
            payload = self._real_feedback[-1]

        return OutgoingMessage(
            sender="reviewer",
            recipient="coder",
            intent="handoff",
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Fake cost model — used only for the "without protection" run
# ---------------------------------------------------------------------------


# Roughly what an LLM API call + output costs per message. Not real.
# We use it only to make the "before" run visceral.
FAKE_COST_PER_MESSAGE_USD = 0.0042


def estimate_cost(messages_sent: int) -> float:
    """Return a fake USD cost for *messages_sent* round-trips."""
    return messages_sent * FAKE_COST_PER_MESSAGE_USD