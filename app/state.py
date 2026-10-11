"""Session store singleton.

Thin wrapper around a pluggable `SessionBackend`. The backend is
selected at import time based on configuration:

  * If ATC_REDIS_URL is set, a RedisBackend is used.
  * Otherwise, an InMemoryBackend is used.

Everything else in the application imports `session_store` from here
and does not know which backend is active.
"""

from __future__ import annotations

import logging

from .config import settings
from .state_backends import (
    InMemoryBackend,
    RedisBackend,
    SessionBackend,
    SessionSnapshot,
)

logger = logging.getLogger("agenttrafficcop.state")


def _select_backend() -> SessionBackend:
    """Choose a backend based on configuration."""
    if settings.redis_url:
        try:
            backend = RedisBackend(
                settings.redis_url,
                ttl_seconds=settings.redis_ttl_seconds,
                key_prefix=settings.redis_key_prefix,
            )
            logger.info(
                "session_backend_selected backend=redis "
                "ttl_seconds=%d key_prefix=%s",
                settings.redis_ttl_seconds,
                settings.redis_key_prefix,
            )
            return backend
        except Exception as e:
            # Fail loudly. A misconfigured Redis should not silently
            # downgrade to in-memory in production — that would break
            # the multi-replica guarantee without warning.
            logger.error(
                "session_backend_redis_init_failed error=%s "
                "falling_back_to=in_memory",
                e,
            )
            # In development, falling back is fine. In production, you
            # probably want to crash instead. Adjust if you prefer.
            if settings.is_production:
                raise
    logger.info("session_backend_selected backend=in_memory")
    return InMemoryBackend()


# Module-level singleton, used by routes and the circuit breaker.
session_store: SessionBackend = _select_backend()

__all__ = ["session_store", "SessionSnapshot", "SessionBackend"]