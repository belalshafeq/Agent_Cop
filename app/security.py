"""Bearer-token authentication for AgentTrafficCop."""

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
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _extract_bearer(authorization: str | None) -> str | None:
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
    """FastAPI dependency: verify the incoming API key."""
    if not settings.auth_enabled and not _has_db_keys():
        logger.warning("auth_misconfigured no api keys configured")
        _audit_auth_failure(reason="auth_not_configured")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_NOT_CONFIGURED", "message": "Server has no API keys configured."},
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    token = _extract_bearer(authorization)
    if token is None:
        _audit_auth_failure(reason="missing_or_malformed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_MISSING", "message": "Missing or malformed Authorization header."},
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    # 1. Check the Database
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT fingerprint FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (token_hash,))
            row = cursor.fetchone()
            if row:
                return row["fingerprint"]
    except Exception as e:
        logger.error("db_auth_check_failed error=%s", e)
        raise HTTPException(status_code=500, detail="Auth system error.")

    # 2. Fallback to .env
    matched_fingerprint: str | None = None
    for candidate in settings.api_keys:
        if secrets.compare_digest(token, candidate):
            matched_fingerprint = _fingerprint(candidate)

    if matched_fingerprint is None:
        _audit_auth_failure(reason="invalid_key", token_fingerprint=_fingerprint(token))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_INVALID", "message": "Invalid API key."},
            headers={"WWW-Authenticate": 'Bearer realm="agenttrafficcop"'},
        )

    return matched_fingerprint


def _has_db_keys() -> bool:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM api_keys LIMIT 1")
            return cursor.fetchone() is not None
    except Exception:
        return False


def _audit_auth_failure(*, reason: str, token_fingerprint: str | None = None) -> None:
    try:
        audit.log_auth_failed(reason=reason, api_key_fingerprint=token_fingerprint)
    except Exception:
        pass