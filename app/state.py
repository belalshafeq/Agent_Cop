"""Session store singleton."""

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
            logger.error(
                "session_backend_redis_init_failed error=%s",
                e,
            )
            if settings.is_production:
                raise
            logger.warning(
                "session_backend_falling_back backend=in_memory "
                "reason=dev_mode"
            )

    backend = InMemoryBackend(
        max_sessions=settings.max_sessions,
        ttl_seconds=settings.session_ttl_seconds,
    )
    logger.info(
        "session_backend_selected backend=in_memory "
        "max_sessions=%d session_ttl_seconds=%d",
        settings.max_sessions,
        settings.session_ttl_seconds,
    )
    return backend


session_store: SessionBackend = _select_backend()

__all__ = ["session_store", "SessionSnapshot", "SessionBackend"]