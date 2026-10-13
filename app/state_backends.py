"""Pluggable session storage backends for AgentTrafficCop.

Two backends ship:
  * InMemoryBackend — default; process-local; lost on restart.
  * RedisBackend    — shared; survives restarts; scales horizontally.

Both implement `SessionBackend`. The rest of the application does not
know or care which is active.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from .config import settings

logger = logging.getLogger("agenttrafficcop.state.backends")


# ---------------------------------------------------------------------------
# Snapshot: an immutable view of a session's state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only view of a session."""

    session_id: str
    step: int
    hashes: List[str]
    created_at: float
    last_seen: float

    def consecutive_repeats(self) -> int:
        """Length of the trailing run of identical hashes."""
        if not self.hashes:
            return 0
        last = self.hashes[-1]
        count = 0
        for h in reversed(self.hashes):
            if h == last:
                count += 1
            else:
                break
        return count

    def cycle_repeats(self, cycle_length: int) -> int:
        """How many full trailing cycles of *cycle_length* exist."""
        if cycle_length <= 0 or len(self.hashes) < cycle_length * 2:
            return 0

        pattern = self.hashes[-cycle_length:]
        prev = self.hashes[-2 * cycle_length : -cycle_length]
        if prev != pattern:
            return 0

        repeats = 2
        idx = len(self.hashes) - 2 * cycle_length
        while idx - cycle_length >= 0:
            block = self.hashes[idx - cycle_length : idx]
            if block == pattern:
                repeats += 1
                idx -= cycle_length
            else:
                break
        return repeats


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class SessionBackend(ABC):
    """Abstract session storage backend. Implementations must be safe
    for concurrent use."""

    @abstractmethod
    def get_or_create(self, session_id: str) -> SessionSnapshot: ...

    @abstractmethod
    def record(self, session_id: str, payload_hash: str) -> SessionSnapshot: ...

    @abstractmethod
    def reset(self, session_id: str) -> bool: ...

    @abstractmethod
    def active_count(self) -> int: ...

    @abstractmethod
    def uptime_seconds(self) -> float: ...

    @abstractmethod
    def list_sessions(self) -> List[SessionSnapshot]:
        """Return a snapshot of every active session.

        Ordering is backend-specific and not guaranteed. Callers that
        need ordering should sort the result themselves.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


@dataclass
class _SessionRecord:
    session_id: str
    step: int = 0
    hashes: Deque[str] = field(
        default_factory=lambda: deque(maxlen=settings.history_window)
    )
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self.session_id,
            step=self.step,
            hashes=list(self.hashes),
            created_at=self.created_at,
            last_seen=self.last_seen,
        )


class InMemoryBackend(SessionBackend):
    """Thread-safe, LRU-capped, in-memory backend."""

    def __init__(self, max_sessions: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._sessions: "OrderedDict[str, _SessionRecord]" = OrderedDict()
        self._max_sessions = max_sessions or settings.max_sessions
        self._started_at = time.time()

    def get_or_create(self, session_id: str) -> SessionSnapshot:
        with self._lock:
            rec = self._sessions.get(session_id)
            if rec is None:
                rec = _SessionRecord(session_id=session_id)
                self._sessions[session_id] = rec
                self._evict_if_needed()
            else:
                self._sessions.move_to_end(session_id)
            return rec.to_snapshot()

    def record(self, session_id: str, payload_hash: str) -> SessionSnapshot:
        with self._lock:
            rec = self._sessions.get(session_id)
            if rec is None:
                rec = _SessionRecord(session_id=session_id)
                self._sessions[session_id] = rec
                self._evict_if_needed()
            else:
                self._sessions.move_to_end(session_id)

            rec.step += 1
            rec.hashes.append(payload_hash)
            rec.last_seen = time.time()
            return rec.to_snapshot()

    def reset(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    def list_sessions(self) -> List[SessionSnapshot]:
        """Return a snapshot of every retained session.

        Ordered by insertion (oldest first). Safe to call concurrently.
        """
        with self._lock:
            return [rec.to_snapshot() for rec in self._sessions.values()]

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)


# ---------------------------------------------------------------------------
# Redis implementation
# ---------------------------------------------------------------------------


class RedisBackend(SessionBackend):
    """Redis-backed session store.

    Requires the `redis` Python package. Instantiated only when
    `settings.redis_url` is set.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = 0,
        key_prefix: str = "atc:",
        connect_retries: int = 15,
        connect_backoff_seconds: float = 1.0,
    ) -> None:
        import redis
        import socket as _socket

        self._redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )

        last_err: Optional[Exception] = None
        for attempt in range(1, connect_retries + 1):
            try:
                self._redis.ping()
                logger.info(
                    "redis_connected attempt=%d url=%s", attempt, url
                )
                break
            except (
                redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
                _socket.gaierror,
                OSError,
            ) as e:
                last_err = e
                if attempt >= connect_retries:
                    logger.error(
                        "redis_connect_gave_up attempts=%d url=%s error=%s",
                        attempt,
                        url,
                        e,
                    )
                    raise
                logger.warning(
                    "redis_connect_retry attempt=%d/%d url=%s error=%s",
                    attempt,
                    connect_retries,
                    url,
                    e,
                )
                time.sleep(connect_backoff_seconds)

        self._ttl = int(ttl_seconds) if ttl_seconds else 0
        self._prefix = key_prefix
        self._started_at = time.time()
        self._history_window = settings.history_window

    # --- Key helpers -------------------------------------------------------

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    def _sk(self, session_id: str, suffix: str) -> str:
        return f"{self._prefix}s:{session_id}:{suffix}"

    def _sessions_set(self) -> str:
        return self._k("sessions")

    # --- Interface ---------------------------------------------------------

    def get_or_create(self, session_id: str) -> SessionSnapshot:
        step_key = self._sk(session_id, "step")
        hashes_key = self._sk(session_id, "hashes")
        created_key = self._sk(session_id, "created")
        seen_key = self._sk(session_id, "seen")

        pipe = self._redis.pipeline()
        pipe.get(step_key)
        pipe.lrange(hashes_key, 0, self._history_window - 1)
        pipe.get(created_key)
        pipe.get(seen_key)
        step_raw, hashes_raw, created_raw, seen_raw = pipe.execute()

        now = time.time()

        if step_raw is None and created_raw is None:
            created_set = self._redis.set(created_key, f"{now}", nx=True)
            if created_set:
                self._redis.sadd(self._sessions_set(), session_id)
                self._apply_ttl(step_key, hashes_key, created_key, seen_key)
            created_raw = self._redis.get(created_key)

        step = int(step_raw) if step_raw is not None else 0
        hashes = list(reversed(hashes_raw)) if hashes_raw else []
        created = float(created_raw) if created_raw else now
        seen = float(seen_raw) if seen_raw else now

        return SessionSnapshot(
            session_id=session_id,
            step=step,
            hashes=hashes,
            created_at=created,
            last_seen=seen,
        )

    def record(self, session_id: str, payload_hash: str) -> SessionSnapshot:
        step_key = self._sk(session_id, "step")
        hashes_key = self._sk(session_id, "hashes")
        created_key = self._sk(session_id, "created")
        seen_key = self._sk(session_id, "seen")

        now = time.time()

        pipe = self._redis.pipeline()
        pipe.incr(step_key)
        pipe.lpush(hashes_key, payload_hash)
        pipe.ltrim(hashes_key, 0, self._history_window - 1)
        pipe.setnx(created_key, f"{now}")
        pipe.set(seen_key, f"{now}")
        pipe.execute()

        self._redis.sadd(self._sessions_set(), session_id)
        self._apply_ttl(step_key, hashes_key, created_key, seen_key)

        return self.get_or_create(session_id)

    def reset(self, session_id: str) -> bool:
        step_key = self._sk(session_id, "step")
        hashes_key = self._sk(session_id, "hashes")
        created_key = self._sk(session_id, "created")
        seen_key = self._sk(session_id, "seen")

        existed = bool(
            self._redis.exists(step_key, hashes_key, created_key, seen_key)
        )
        pipe = self._redis.pipeline()
        pipe.delete(step_key, hashes_key, created_key, seen_key)
        pipe.srem(self._sessions_set(), session_id)
        pipe.execute()
        return existed

    def active_count(self) -> int:
        """Return the number of tracked sessions.

        Fails soft: if Redis is unreachable, returns 0 rather than raising.
        """
        try:
            return int(self._redis.scard(self._sessions_set()))
        except Exception as e:
            logger.warning("redis_active_count_failed error=%s", e)
            return 0

    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    def list_sessions(self) -> List[SessionSnapshot]:
        """Return a snapshot of every session in Redis.

        Uses SMEMBERS to get session IDs, then pipelines get_or_create
        for each. Complexity is O(N) Redis round trips, which is fine
        for the sizes this is designed for (< 100K sessions).
        """
        try:
            ids = self._redis.smembers(self._sessions_set())
        except Exception as e:
            logger.warning("redis_list_sessions_failed error=%s", e)
            return []

        snapshots: List[SessionSnapshot] = []
        for session_id in ids:
            try:
                snapshots.append(self.get_or_create(session_id))
            except Exception as e:
                # Skip sessions that vanish mid-iteration.
                logger.warning(
                    "redis_list_sessions_skip session_id=%s error=%s",
                    session_id,
                    e,
                )
        return snapshots

    # --- Internals ---------------------------------------------------------

    def _apply_ttl(self, *keys: str) -> None:
        if self._ttl <= 0:
            return
        pipe = self._redis.pipeline()
        for k in keys:
            pipe.expire(k, self._ttl)
        pipe.execute()