"""Exception hierarchy for the AgentTrafficCop SDK.

Every error the SDK raises inherits from `AgentTrafficCopError`, so a
caller can write:

    try:
        await guard.check(...)
    except AgentTrafficCopError as e:
        log(e.error_code, e.session_id, e.diagnostics)

The subclasses exist so callers can react to *specific* failures:

    try:
        await guard.check(...)
    except LoopDetected:
        halt_session()
    except StepLimitExceeded:
        alert_ops()
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AgentTrafficCopError(Exception):
    """Base class for all SDK exceptions.

    Attributes:
        message:     Human-readable error message.
        error_code:  Machine-readable code from the server (e.g.
                     "REPEATED_PAYLOAD_LOOP"). None if the SDK raised
                     this locally without a server response.
        session_id:  Session the error relates to, if known.
        diagnostics: Structured details from the server, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[str] = None,
        session_id: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.session_id = session_id
        self.diagnostics = diagnostics or {}

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"error_code={self.error_code!r}, "
            f"session_id={self.session_id!r}, "
            f"message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Governance errors (server returned 422 with a known error code)
# ---------------------------------------------------------------------------


class LoopDetected(AgentTrafficCopError):
    """The circuit breaker tripped on a repeated or cyclic payload.

    The server sets `error_code` to one of:
        REPEATED_PAYLOAD_LOOP  — same hash seen too many times consecutively
        CYCLIC_PAYLOAD_LOOP    — short repeating cycle detected (A↔B)
    """


class StepLimitExceeded(AgentTrafficCopError):
    """The session exceeded the configured maximum number of steps."""


class SchemaRejected(AgentTrafficCopError):
    """The server rejected the message as failing schema validation.

    This means the message didn't match the AgentMessage contract.
    Usually a client-side bug — the payload was malformed before it
    was even sent.
    """


class TierLimitExceeded(AgentTrafficCopError):
    """The API key exceeded its monthly message limit (429)."""


class AgentLimitExceeded(AgentTrafficCopError):
    """The API key exceeded its unique-agent limit (402)."""


# ---------------------------------------------------------------------------
# Transport / auth errors (server returned 401/403 or the request failed)
# ---------------------------------------------------------------------------


class AuthError(AgentTrafficCopError):
    """The server rejected the API key (401).

    Possible causes:
        * No key configured on the server (fail-closed).
        * Wrong key sent by the client.
        * Malformed Authorization header.
    """


class AdminRequired(AgentTrafficCopError):
    """The endpoint requires an admin API key (403).

    Raised when calling admin endpoints with a non-admin key.
    """


class ConnectionError(AgentTrafficCopError):
    """Could not reach the AgentTrafficCop server.

    Wraps the underlying transport error (httpx.ConnectError,
    httpx.TimeoutException, etc.) as an SDK-level exception.
    """


# ---------------------------------------------------------------------------
# Internal helpers (used by the client to map server responses)
# ---------------------------------------------------------------------------


# Map server error codes -> SDK exception classes.
_ERROR_CODE_MAP = {
    "REPEATED_PAYLOAD_LOOP": LoopDetected,
    "CYCLIC_PAYLOAD_LOOP": LoopDetected,
    "STEP_LIMIT_EXCEEDED": StepLimitExceeded,
    "SCHEMA_VALIDATION_FAILED": SchemaRejected,
    "CIRCUIT_BREAKER_TRIPPED": LoopDetected,  # fallback for base breaker
    "TIER_LIMIT_EXCEEDED": TierLimitExceeded,
    "AGENT_LIMIT_EXCEEDED": AgentLimitExceeded,
}


def exception_from_response(body: Dict[str, Any]) -> AgentTrafficCopError:
    """Build the right SDK exception from a 422/429/402 response body.

    Falls back to the base class for unknown error codes so the SDK
    never crashes on a server that adds new codes.
    """
    error_code = body.get("error")
    message = body.get("message", "AgentTrafficCop rejected the message.")
    session_id = body.get("session_id")
    diagnostics = body.get("diagnostics") or {}

    exc_class = _ERROR_CODE_MAP.get(error_code, AgentTrafficCopError)
    return exc_class(
        message,
        error_code=error_code,
        session_id=session_id,
        diagnostics=diagnostics,
    )