"""Per-API-key usage metering."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .config import settings
from .db import get_db

logger = logging.getLogger("agenttrafficcop.usage")


# ---------------------------------------------------------------------------
# Month bucket helpers
# ---------------------------------------------------------------------------


def current_month_bucket() -> str:
    """Return the current year-month as a stable string, e.g. `2026-10`."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def next_month_start_utc() -> str:
    """Return the ISO-8601 UTC timestamp of the next month's first moment."""
    now = datetime.now(timezone.utc)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    next_start = datetime(year, month, 1, tzinfo=timezone.utc)
    return next_start.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class UsageBackend:
    """Abstract usage counter backend."""

    def increment(self, fingerprint: str, amount: int = 1) -> int:
        raise NotImplementedError

    def get(self, fingerprint: str) -> int:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryUsageBackend(UsageBackend):
    """Thread-safe, in-process usage counter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # {(fingerprint, month): count}
        self._data: Dict[Tuple[str, str], int] = {}

    def increment(self, fingerprint: str, amount: int = 1) -> int:
        key = (fingerprint, current_month_bucket())
        with self._lock:
            self._data[key] = self._data.get(key, 0) + int(amount)
            return self._data[key]

    def get(self, fingerprint: str) -> int:
        key = (fingerprint, current_month_bucket())
        with self._lock:
            return self._data.get(key, 0)


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


class RedisUsageBackend(UsageBackend):
    """Redis-backed usage counter."""

    def __init__(self, redis_client, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _key(self, fingerprint: str) -> str:
        return f"{self._prefix}{fingerprint}:{current_month_bucket()}"

    def increment(self, fingerprint: str, amount: int = 1) -> int:
        try:
            key = self._key(fingerprint)
            new_value = int(self._redis.incrby(key, int(amount)))
            self._redis.expire(key, 62 * 24 * 60 * 60)
            return new_value
        except Exception as e:
            logger.warning("usage_increment_failed error=%s", e)
            return 0

    def get(self, fingerprint: str) -> int:
        try:
            raw = self._redis.get(self._key(fingerprint))
            return int(raw) if raw is not None else 0
        except Exception as e:
            logger.warning("usage_get_failed error=%s", e)
            return 0


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


def _build_backend() -> UsageBackend:
    """Choose a backend based on configuration."""
    if settings.redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=3.0,
                socket_connect_timeout=3.0,
            )
            client.ping()
            logger.info(
                "usage_backend_selected backend=redis prefix=%s",
                settings.usage_key_prefix,
            )
            return RedisUsageBackend(client, settings.usage_key_prefix)
        except Exception as e:
            logger.warning(
                "usage_backend_redis_failed error=%s falling_back=in_memory",
                e,
            )
    logger.info("usage_backend_selected backend=in_memory")
    return InMemoryUsageBackend()


usage_backend: UsageBackend = _build_backend()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_message(fingerprint: str) -> int:
    """Increment the caller's usage counter. Returns the new value."""
    return usage_backend.increment(fingerprint, 1)


def get_usage(fingerprint: str) -> int:
    """Return the caller's usage count for the current month."""
    return usage_backend.get(fingerprint)


def get_tier_and_limit(fingerprint: str) -> Tuple[str, int]:
    """Return (tier, limit) for the caller, given their fingerprint.
    
    Checks the SQLite DB first. If not found, falls back to .env settings.
    """
    # --- 1. Check the Database ---
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tier FROM api_keys WHERE fingerprint = ? AND revoked_at IS NULL", 
                (fingerprint,)
            )
            row = cursor.fetchone()
            if row:
                tier = row["tier"]
                return tier, settings.limit_for_tier(tier)
    except Exception as e:
        logger.error("db_tier_check_failed error=%s", e)

    # --- 2. Fallback to .env (Legacy/Admin Keys) ---
    from hashlib import sha256

    def _fp(k: str) -> str:
        return sha256(k.encode()).hexdigest()[:8]

    for tier_name, key_list in (
        ("enterprise", settings.enterprise_api_keys),
        ("team", settings.team_api_keys + settings.api_keys),
        ("pro", settings.pro_api_keys),
        ("free", settings.free_api_keys),
    ):
        for k in key_list:
            if _fp(k) == fingerprint:
                return tier_name, settings.limit_for_tier(tier_name)

    return "free", settings.limit_for_tier("free")


# ---------------------------------------------------------------------------
# Usage snapshot
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass(frozen=True)
class UsageSnapshot:
    """Everything a caller needs to know about their usage."""

    api_key_fingerprint: str
    tier: str
    messages_this_month: int
    tier_limit: int          # 0 = unlimited
    remaining: Optional[int] # None if unlimited
    reset_at: str
    enforcement_enabled: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "api_key_fingerprint": self.api_key_fingerprint,
            "tier": self.tier,
            "messages_this_month": self.messages_this_month,
            "tier_limit": self.tier_limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "enforcement_enabled": self.enforcement_enabled,
        }


def snapshot(fingerprint: str) -> UsageSnapshot:
    """Build a UsageSnapshot for the given fingerprint."""
    tier, limit = get_tier_and_limit(fingerprint)
    used = get_usage(fingerprint)
    remaining = None if limit == 0 else max(0, limit - used)
    return UsageSnapshot(
        api_key_fingerprint=fingerprint,
        tier=tier,
        messages_this_month=used,
        tier_limit=limit,
        remaining=remaining,
        reset_at=next_month_start_utc(),
        enforcement_enabled=settings.usage_enforcement_enabled,
    )


def is_over_limit(fingerprint: str) -> Tuple[bool, str, int, int]:
    """Check whether the caller has exceeded their tier limit."""
    if not settings.usage_enforcement_enabled:
        tier, limit = get_tier_and_limit(fingerprint)
        return False, tier, get_usage(fingerprint), limit

    tier, limit = get_tier_and_limit(fingerprint)
    if limit == 0:
        return False, tier, get_usage(fingerprint), limit

    used = get_usage(fingerprint)
    return (used >= limit), tier, used, limit