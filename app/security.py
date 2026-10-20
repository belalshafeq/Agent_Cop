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
from .db import get_db

logger = logging.getLogger("agenttrafficcop.security")


def _fingerprint(value: str) -> str:
    """Return a short, non-reversible fingerprint of a secret."""
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
    
    Checks the SQLite database FIRST. If not found, falls back to .env keys.
    Returns the *fingerprint* of the matched key.
    """
    if not authorization:
        _audit_auth_failure(reason="missing_or_malformed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_MISSING", "message": "Missing Authorization header."},
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    token = _extract_bearer(authorization)
    if token is None:
        _audit_auth_failure(reason="missing_or_malformed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_MALFORMED", "message": "Invalid Bearer format."},
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    # --- 1. Check the Database ---
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT fingerprint FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", 
                (token_hash,)
            )
            row = cursor.fetchone()
            if row:
                # Found in DB! Return the fingerprint.
                return row["fingerprint"]
    except Exception as e:
        logger.error("db_auth_check_failed error=%s", e)
        # If DB fails, we fail closed (do not fall back to .env)
        raise HTTPException(status_code=500, detail="Auth system error.")

    # --- 2. Fallback to .env (Legacy/Admin Keys) ---
    matched_fingerprint: str | None = None
    for candidate in settings.api_keys:
        if secrets.compare_digest(token, candidate):
            matched_fingerprint = _fingerprint(candidate)

    if matched_fingerprint is None:
        logger.warning("auth_rejected token_fingerprint=%s", _fingerprint(token))
        _audit_auth_failure(
            reason="invalid_key",
            token_fingerprint=_fingerprint(token),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_INVALID", "message": "Invalid API key."},
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