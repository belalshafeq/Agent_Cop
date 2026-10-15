"""Pydantic v2 strict data contracts for AgentTrafficCop."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_INTENTS = (
    "request",
    "response",
    "handoff",
    "tool_call",
    "tool_result",
    "error",
)


# ---------------------------------------------------------------------------
# Incoming
# ---------------------------------------------------------------------------


class AgentMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=False,
    )

    session_id: str = Field(..., min_length=1, max_length=128)
    sender: str = Field(..., min_length=1, max_length=64)
    recipient: str = Field(..., min_length=1, max_length=64)
    intent: Literal[
        "request", "response", "handoff",
        "tool_call", "tool_result", "error",
    ] = Field(...)
    payload: Dict[str, Any] = Field(...)
    metadata: Optional[Dict[str, Any]] = Field(default=None)

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
        other = info.data.get("sender") if info.field_name == "recipient" else None
        if info.field_name == "recipient" and other is not None and v == other:
            raise ValueError("sender and recipient must differ")
        return v


# ---------------------------------------------------------------------------
# Outgoing
# ---------------------------------------------------------------------------


class RouteApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: Literal[True] = True
    session_id: str
    step: int = Field(..., ge=1)
    payload_hash: str = Field(..., min_length=64, max_length=64)
    message: str = "Message approved and forwarded."


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str = "AgentTrafficCop"
    version: str
    active_sessions: int = Field(..., ge=0)
    uptime_seconds: float = Field(..., ge=0.0)


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_fingerprint: str = Field(..., min_length=1, max_length=64)
    tier: Literal["free", "pro", "team", "enterprise"]
    messages_this_month: int = Field(..., ge=0)
    tier_limit: int = Field(..., ge=0, description="0 means unlimited.")
    remaining: Optional[int] = Field(
        default=None,
        description="None when the tier is unlimited.",
    )
    reset_at: str = Field(..., min_length=1)
    enforcement_enabled: bool


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    step: int = Field(..., ge=0)
    hash_count: int = Field(..., ge=0)
    created_at: float = Field(..., ge=0.0)
    last_seen: float = Field(..., ge=0.0)
    age_seconds: float = Field(..., ge=0.0)
    idle_seconds: float = Field(..., ge=0.0)


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    returned: int = Field(..., ge=0)
    sessions: List[SessionSummary]


class SessionDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    step: int = Field(..., ge=0)
    hash_count: int = Field(..., ge=0)
    hashes: List[str]
    created_at: float = Field(..., ge=0.0)
    last_seen: float = Field(..., ge=0.0)
    age_seconds: float = Field(..., ge=0.0)
    idle_seconds: float = Field(..., ge=0.0)


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    older_than_seconds: int = Field(..., ge=1)


class CleanupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expired: int = Field(..., ge=0)
    remaining: int = Field(..., ge=0)