"""Bearer-token authentication for AgentTrafficCop.

Design principles:
  * Fail closed — no configured keys means no access, ever.
  * Constant-time comparison — no timing side-channels.
  * No secrets in logs — only an opaque fingerprint for debugging.
  * Standard HTTP semantics — 401 with WWW-Authenticate per RFC 6750.
  * Every failure writes to the audit log.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from .audit import audit
from .config import settings

logger = logging.getLogger("agenttrafficcop.security")


def _fingerprint(value: str) -> str:
    """Return a short, non-reversible fingerprint of a secret.

    Used ONLY for log correlation. The full secret never appears in
    logs, and the fingerprint is a truncated SHA-256 — you cannot
    recover the original from it.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _extract_bearer(authorization: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    if not token:
        return None
    return token


def verify_api_key(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI dependency: verify the incoming API key.

    Returns the *fingerprint* of the matched key (never the key itself).

    Raises:
        HTTPException(401) on missing, malformed, or invalid key.
    """
    # Fail-closed: if no keys are configured, refuse everything.
    if not settings.auth_enabled:
        logger.warning("auth_misconfigured no api keys configured")
        _audit_auth_failure(reason="auth_not_configured")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "AUTH_NOT_CONFIGURED",
                "message": (
                    "Server has no API keys configured. Set ATC_API_KEYS "
                    "in the environment to enable access."
                ),
            },
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    token = _extract_bearer(authorization)
    if token is None:
        logger.warning("auth_missing_or_malformed")
        _audit_auth_failure(reason="missing_or_malformed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "AUTH_MISSING",
                "message": "Missing or malformed Authorization header.",
            },
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    matched_fingerprint: str | None = None
    for candidate in settings.api_keys:
        if secrets.compare_digest(token, candidate):
            matched_fingerprint = _fingerprint(candidate)

    if matched_fingerprint is None:
        logger.warning(
            "auth_rejected token_fingerprint=%s", _fingerprint(token)
        )
        _audit_auth_failure(
            reason="invalid_key",
            token_fingerprint=_fingerprint(token),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "AUTH_INVALID",
                "message": "Invalid API key.",
            },
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    return matched_fingerprint


def _audit_auth_failure(
    *,
    reason: str,
    token_fingerprint: str | None = None,
) -> None:
    """Write an auth failure to the audit log. Never raises."""
    try:
        audit.log_auth_failed(
            reason=reason,
            api_key_fingerprint=token_fingerprint,
        )
    except Exception:
        pass