"""Session store singleton.

This module is a thin wrapper around a pluggable `SessionBackend`
(defined in `state_backends.py`). It:
  * Chooses the backend at import time based on configuration
  * Exposes a single module-level `session_store` instance
  * Re-exports `SessionSnapshot` so callers can import it from here

Backends handle all persistence. This module contains no logic.
"""

from __future__ import annotations

import logging
import os

from .state_backends import (
    InMemoryBackend,
    SessionBackend,
    SessionSnapshot,
)

logger = logging.getLogger("agenttrafficcop.state")


def _select_backend() -> SessionBackend:
    """Choose a backend based on environment.

    Batch 1: only the in-memory backend exists, so we always use it.
    Batch 2 will add:  if ATC_REDIS_URL is set, use RedisBackend.
    """
    redis_url = os.getenv("ATC_REDIS_URL", "").strip()
    if redis_url:
        # Placeholder for Batch 2. For now, log and fall back so no
        # one is surprised if they set the var early.
        logger.warning(
            "atc_redis_url_set_but_backend_not_implemented "
            "url=%s falling_back_to=in_memory",
            redis_url,
        )
    return InMemoryBackend()


# Module-level singleton, used by routes and the circuit breaker.
session_store: SessionBackend = _select_backend()

__all__ = ["session_store", "SessionSnapshot", "SessionBackend"]