"""Pluggable session storage backends for AgentTrafficCop.

Defines an abstract `SessionBackend` interface and an in-memory
implementation. A Redis-backed implementation lands in a later batch
and satisfies the same interface — the rest of the application does
not know or care which backend is active.

Design rules:
  * Backends exchange primitives (str/int/float/list), never custom
    objects. This keeps a future Redis implementation trivial to write.
  * All public methods are safe to call from any thread.
  * Backends are stateless with respect to configuration — settings
    are injected at construction time.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from .config import settings


# ---------------------------------------------------------------------------
# Snapshot: an immutable view of a session's state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only view of a session.

    Returned by backends on every operation. The circuit breaker and
    routes consume this — they never mutate it. Mutation happens only
    through backend methods, which return a *new* snapshot.
    """

    session_id: str
    step: int
    hashes: List[str]
    created_at: float
    last_seen: float

    # --- Derived helpers (pure functions, no I/O) --------------------------

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
    """Abstract session storage backend.

    Implementations must be safe for concurrent use.
    """

    @abstractmethod
    def get_or_create(self, session_id: str) -> SessionSnapshot:
        """Return the current state, creating it if it doesn't exist."""

    @abstractmethod
    def record(self, session_id: str, payload_hash: str) -> SessionSnapshot:
        """Increment the step count, append the hash, return the new state.

        Must be atomic with respect to other calls for the same session.
        """

    @abstractmethod
    def reset(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""

    @abstractmethod
    def active_count(self) -> int:
        """Number of sessions currently retained."""

    @abstractmethod
    def uptime_seconds(self) -> float:
        """Seconds since the backend was created."""


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


@dataclass
class _SessionRecord:
    """Internal mutable record — not exposed outside this module."""

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
    """Thread-safe, LRU-capped, in-memory backend.

    This is the default backend. Sessions live in this process only —
    they do not survive a restart and are not shared across replicas.
    """

    def __init__(self, max_sessions: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._sessions: "OrderedDict[str, _SessionRecord]" = OrderedDict()
        self._max_sessions = max_sessions or settings.max_sessions
        self._started_at = time.time()

    # --- Interface implementation -----------------------------------------

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

    # --- Internals ---------------------------------------------------------

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)