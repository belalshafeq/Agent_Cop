"""Typed models returned by the AgentTrafficCop SDK.

These mirror the response schemas of the server's /v1/route endpoint,
but live in the SDK so callers get typed attributes and IDE
autocomplete instead of raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class RouteResult:
    """A successful approval returned by POST /v1/route.

    Attributes:
        approved:      Always True on success. Present for symmetry with
                       the server response.
        session_id:    The session this message belongs to.
        step:          1-indexed step number this message was assigned.
        payload_hash:  SHA-256 hex digest of the payload (64 chars).
        message:       Human-readable confirmation from the server.
        raw:           The original response dict, in case a caller needs
                       a field the SDK doesn't model yet.
    """

    approved: bool
    session_id: str
    step: int
    payload_hash: str
    message: str
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, body: Dict[str, Any]) -> "RouteResult":
        """Build a RouteResult from a parsed JSON response body.

        Raises ValueError if required fields are missing or malformed.
        """
        try:
            return cls(
                approved=bool(body["approved"]),
                session_id=str(body["session_id"]),
                step=int(body["step"]),
                payload_hash=str(body["payload_hash"]),
                message=str(body.get("message", "")),
                raw=dict(body),
            )
        except KeyError as e:
            raise ValueError(
                f"Server response missing required field: {e}"
            ) from e
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Server response had an unexpected shape: {e}"
            ) from e