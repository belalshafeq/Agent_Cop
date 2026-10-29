"""Per-API-key usage metering."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Dict, Optional, Tuple

from .config import settings
from .db import get_db

logger = logging.getLogger("agenttrafficcop.usage")

# FIX (H3): prune stale month buckets only when the dict grows past this.
# Cheap enough to check per-write; avoids unbounded growth over months.
_PRUNE_THRESHOLD = 100_000


# ---------------------------------------------------------------------------
# Month bucket helpers
# ---------------------------------------------------------------------------


def current_month_bucket() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def next_month_start_utc() -> str:
    now = datetime.now(timezone.utc)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    next_start = datetime(year, month, 1, tzinfo=timezone.utc)
    return next_start.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class UsageBackend:
    def increment(self, fingerprint: str, amount: int = 1) -> int:
        raise NotImplementedError

    def get(self, fingerprint: str) -> int:
        raise NotImplementedError

    def add_agents(self, key: str, agents: set[str]) -> int:
        raise NotImplementedError

    def get_agents_set(self, key: str) -> set[str]:
        raise NotImplementedError

    # FIX (C8): atomic check-and-add. Returns (allowed, newly_added_agents).
    # The old code did read → check → later → write, which meant two
    # concurrent requests could both pass the limit check and both record,
    # blowing past the tier's agent cap.
    def check_and_add_agents(
        self, key: str, agents: set[str], limit: int,
    ) -> Tuple[bool, set[str]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryUsageBackend(UsageBackend):
    """Thread-safe, in-process usage counter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[Tuple[str, str], int] = {}
        self._agents: Dict[Tuple[str, str], set[str]] = {}

    def _maybe_prune(self, current_month: str) -> None:
        """Drop entries from prior months. Caller must hold the lock."""
        if len(self._data) > _PRUNE_THRESHOLD:
            stale = [k for k in self._data if k[1] < current_month]
            for k in stale:
                del self._data[k]
        if len(self._agents) > _PRUNE_THRESHOLD:
            stale = [k for k in self._agents if k[1] < current_month]
            for k in stale:
                del self._agents[k]

    def increment(self, fingerprint: str, amount: int = 1) -> int:
        month = current_month_bucket()
        key = (fingerprint, month)
        with self._lock:
            self._data[key] = self._data.get(key, 0) + int(amount)
            self._maybe_prune(month)
            return self._data[key]

    def get(self, fingerprint: str) -> int:
        key = (fingerprint, current_month_bucket())
        with self._lock:
            return self._data.get(key, 0)

    def add_agents(self, key: str, agents: set[str]) -> int:
        month = current_month_bucket()
        k = (key, month)
        with self._lock:
            if k not in self._agents:
                self._agents[k] = set()
            self._agents[k].update(agents)
            self._maybe_prune(month)
            return len(self._agents[k])

    def get_agents_set(self, key: str) -> set[str]:
        k = (key, current_month_bucket())
        with self._lock:
            return set(self._agents.get(k, set()))

    def check_and_add_agents(
        self, key: str, agents: set[str], limit: int,
    ) -> Tuple[bool, set[str]]:
        month = current_month_bucket()
        k = (key, month)
        with self._lock:
            current = self._agents.get(k, set())
            newly_added = agents - current
            if limit > 0 and len(current) + len(newly_added) > limit:
                return False, newly_added
            self._agents[k] = current | agents
            self._maybe_prune(month)
            return True, newly_added


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

    def _agents_key(self, fingerprint: str) -> str:
        return f"{self._prefix}agents:{fingerprint}:{current_month_bucket()}"

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

    def add_agents(self, key: str, agents: set[str]) -> int:
        try:
            rk = self._agents_key(key)
            if agents:
                self._redis.sadd(rk, *agents)
            self._redis.expire(rk, 62 * 24 * 60 * 60)
            return int(self._redis.scard(rk))
        except Exception as e:
            logger.warning("usage_add_agents_failed error=%s", e)
            return 0

    def get_agents_set(self, key: str) -> set[str]:
        try:
            return set(self._redis.smembers(self._agents_key(key)))
        except Exception as e:
            logger.warning("usage_get_agents_failed error=%s", e)
            return set()

    # FIX (C8): atomic check-and-add via Lua. Runs server-side on Redis,
    # so concurrent requests are serialized — no TOCTOU window.
    _CHECK_AND_ADD_LUA = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local current = redis.call('SCARD', key)
    local new_count = 0
    for i = 2, #ARGV do
        if redis.call('SISMEMBER', key, ARGV[i]) == 0 then
            new_count = new_count + 1
        end
    end
    if limit > 0 and (current + new_count) > limit then
        return {0, current}
    end
    for i = 2, #ARGV do
        redis.call('SADD', key, ARGV[i])
    end
    redis.call('EXPIRE', key, 5356800)  -- 62 days
    return {1, current + new_count}
    """

    def check_and_add_agents(
        self, key: str, agents: set[str], limit: int,
    ) -> Tuple[bool, set[str]]:
        if not agents:
            return True, set()
        try:
            args = [str(limit), *sorted(agents)]
            result = self._redis.eval(
                self._CHECK_AND_ADD_LUA, 1, self._agents_key(key), *args,
            )
            allowed = bool(int(result[0]))
            return allowed, agents if allowed else set()
        except Exception as e:
            logger.warning("usage_check_and_add_agents_failed error=%s", e)
            # Fail open — a Redis hiccup shouldn't reject real customers.
            return True, agents


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


def _build_backend() -> UsageBackend:
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
                "usage_backend_redis_failed error=%s falling_back=in_memory", e,
            )
    logger.info("usage_backend_selected backend=in_memory")
    return InMemoryUsageBackend()


usage_backend: UsageBackend = _build_backend()


# ---------------------------------------------------------------------------
# Caching layer
# ---------------------------------------------------------------------------


@lru_cache(maxsize=10_000)
def _resolve_usage_key(fingerprint: str) -> str:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM api_keys WHERE fingerprint = ? AND revoked_at IS NULL",
                (fingerprint,),
            )
            row = cursor.fetchone()
            if row:
                return f"user_{row['user_id']}"
    except Exception as e:
        logger.error("resolve_usage_key_failed error=%s", e)
    return f"key_{fingerprint}"


@lru_cache(maxsize=10_000)
def get_tier_and_limit(fingerprint: str) -> Tuple[str, int]:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tier FROM api_keys WHERE fingerprint = ? AND revoked_at IS NULL",
                (fingerprint,),
            )
            row = cursor.fetchone()
            if row:
                tier = row["tier"]
                return tier, settings.limit_for_tier(tier)
    except Exception as e:
        logger.error("db_tier_check_failed error=%s", e)

    from hashlib import sha256

    def _fp(k: str) -> str:
        return sha256(k.encode()).hexdigest()[:8]

    for tier_name, key_list in (
        ("team", settings.team_api_keys + settings.api_keys),
        ("pro", settings.pro_api_keys),
        ("free", settings.free_api_keys),
    ):
        for k in key_list:
            if _fp(k) == fingerprint:
                return tier_name, settings.limit_for_tier(tier_name)

    return "free", settings.limit_for_tier("free")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_message(fingerprint: str) -> int:
    resolved_key = _resolve_usage_key(fingerprint)
    return usage_backend.increment(resolved_key, 1)


def get_usage(fingerprint: str) -> int:
    resolved_key = _resolve_usage_key(fingerprint)
    return usage_backend.get(resolved_key)


def record_agents(fingerprint: str, sender: str, recipient: str) -> int:
    resolved_key = _resolve_usage_key(fingerprint)
    return usage_backend.add_agents(resolved_key, {sender, recipient})


def get_agents_set(fingerprint: str) -> set[str]:
    resolved_key = _resolve_usage_key(fingerprint)
    return usage_backend.get_agents_set(resolved_key)


# FIX (C8): the new atomic entry point used by routes.py. Checks the limit
# and reserves the agents in one backend call so concurrent requests can't
# both pass the check and both add.
def check_and_add_agents_atomic(
    fingerprint: str, agents: set[str], limit: int,
) -> Tuple[bool, set[str]]:
    resolved_key = _resolve_usage_key(fingerprint)
    return usage_backend.check_and_add_agents(resolved_key, agents, limit)


# ---------------------------------------------------------------------------
# Usage snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageSnapshot:
    api_key_fingerprint: str
    tier: str
    messages_this_month: int
    tier_limit: int
    remaining: Optional[int]
    reset_at: str
    enforcement_enabled: bool
    agents_this_month: int = 0
    agent_limit: int = 5

    def to_dict(self) -> Dict[str, object]:
        return {
            "api_key_fingerprint": self.api_key_fingerprint,
            "tier": self.tier,
            "messages_this_month": self.messages_this_month,
            "tier_limit": self.tier_limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "enforcement_enabled": self.enforcement_enabled,
            "agents_this_month": self.agents_this_month,
            "agent_limit": self.agent_limit,
        }


def snapshot(fingerprint: str) -> UsageSnapshot:
    tier, limit = get_tier_and_limit(fingerprint)
    used = get_usage(fingerprint)
    remaining = None if limit == 0 else max(0, limit - used)
    agents_seen = get_agents_set(fingerprint)
    return UsageSnapshot(
        api_key_fingerprint=fingerprint,
        tier=tier,
        messages_this_month=used,
        tier_limit=limit,
        remaining=remaining,
        reset_at=next_month_start_utc(),
        enforcement_enabled=settings.usage_enforcement_enabled,
        agents_this_month=len(agents_seen),
        agent_limit=settings.agent_limit_for_tier(tier),
    )


def is_over_limit(fingerprint: str) -> Tuple[bool, str, int, int]:
    if not settings.usage_enforcement_enabled:
        tier, limit = get_tier_and_limit(fingerprint)
        return False, tier, get_usage(fingerprint), limit

    tier, limit = get_tier_and_limit(fingerprint)
    if limit == 0:
        return False, tier, get_usage(fingerprint), limit

    used = get_usage(fingerprint)
    return (used >= limit), tier, used, limit