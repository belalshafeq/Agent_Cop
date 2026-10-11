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
    """Choose a backend based on configuration.

    If Redis is configured but unreachable, we retry a few times
    (handled inside RedisBackend). If it still fails:
      * In production: raise — fail loudly, do not silently degrade.
      * In development: log and fall back to in-memory so local dev
        is not blocked by a missing Redis.
    """
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
            logger.error(
                "session_backend_redis_init_failed error=%s",
                e,
            )
            if settings.is_production:
                # In production, a misconfigured Redis is a fatal error.
                # Silent fallback to in-memory would break the
                # multi-replica guarantee without warning.
                raise
            logger.warning(
                "session_backend_falling_back backend=in_memory "
                "reason=dev_mode"
            )
    logger.info("session_backend_selected backend=in_memory")
    return InMemoryBackend()


# Module-level singleton, used by routes and the circuit breaker.
session_store: SessionBackend = _select_backend()

__all__ = ["session_store", "SessionSnapshot", "SessionBackend"]