"""Pydantic v2 strict data contracts for AgentTrafficCop.

These schemas form the governance shield: any payload that fails
validation is rejected with a structured HTTP 422 response *before*
it can reach a downstream agent.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_INTENTS = (
    "request",
    "response",
    "handoff",
    "tool_call",
    "tool_result",
    "error",
)

# ---------------------------------------------------------------------------
# Incoming: agent-to-agent message
# ---------------------------------------------------------------------------


class AgentMessage(BaseModel):
    """A single agent-to-agent handoff message.

    Strict mode:
      * Unknown fields are rejected (extra="forbid").
      * All core fields are required.
      * Strings are stripped of surrounding whitespace.
      * Field lengths are bounded to prevent memory abuse.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=False,
    )

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Unique identifier for the execution session / graph run.",
    )
    sender: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Identifier of the emitting agent.",
    )
    recipient: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Identifier of the target agent.",
    )
    intent: Literal[
        "request",
        "response",
        "handoff",
        "tool_call",
        "tool_result",
        "error",
    ] = Field(
        ...,
        description="Semantic intent of the message.",
    )
    payload: Dict[str, Any] = Field(
        ...,
        description="Structured payload handed off between agents.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional non-semantic metadata (trace ids, tags).",
    )

    # --- Validators ---------------------------------------------------------

    @field_validator("payload")
    @classmethod
    def _payload_must_not_be_empty(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not v:
            raise ValueError("payload must not be empty")
        return v

    @field_validator("sender", "recipient", "session_id")
    @classmethod
    def _must_not_be_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()

    @field_validator("sender", "recipient")
    @classmethod
    def _no_self_handoff(cls, v: str, info) -> str:
        # Cross-field check: reject sender == recipient. We use the
        # already-validated sibling value from `info.data` when available.
        other = info.data.get("sender") if info.field_name == "recipient" else None
        if info.field_name == "recipient" and other is not None and v == other:
            raise ValueError("sender and recipient must differ")
        return v


# ---------------------------------------------------------------------------
# Outgoing: successful approval
# ---------------------------------------------------------------------------


class RouteApproval(BaseModel):
    """Successful routing decision returned by the middleware."""

    model_config = ConfigDict(extra="forbid")

    approved: Literal[True] = True
    session_id: str
    step: int = Field(..., ge=1)
    payload_hash: str = Field(..., min_length=64, max_length=64)
    message: str = "Message approved and forwarded."


# ---------------------------------------------------------------------------
# Outgoing: health probe
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """System health probe response."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str = "AgentTrafficCop"
    version: str
    active_sessions: int = Field(..., ge=0)
    uptime_seconds: float = Field(..., ge=0.0)