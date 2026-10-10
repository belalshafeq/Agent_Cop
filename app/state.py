"""Thread-safe in-memory session state engine.

Tracks per-session execution depth, payload hash history, and
last-seen timestamps. Uses a lock-protected OrderedDict to provide
LRU eviction when the session cap is reached.

Design goals:
  * Correctness under concurrency (single RLock, no deadlocks).
  * Bounded memory (deque maxlen + LRU eviction).
  * Zero external dependencies (stdlib only).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, List

from .config import settings


@dataclass
class SessionState:
    """Mutable per-session bookkeeping.

    Each session tracks:
      * `step`     — how many messages have passed through.
      * `hashes`   — the last N payload hashes (fixed-size deque).
      * timestamps — creation and last activity (for observability).
    """

    session_id: str
    step: int = 0
    hashes: Deque[str] = field(
        default_factory=lambda: deque(maxlen=settings.history_window)
    )
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def record(self, payload_hash: str) -> int:
        """Increment the step count, record the hash, return the new step.

        This is the *only* mutation point for a session's history. It
        is intentionally synchronous and side-effect-free apart from
        updating the in-memory fields.
        """
        self.step += 1
        self.hashes.append(payload_hash)
        self.last_seen = time.time()
        return self.step

    def consecutive_repeats(self) -> int:
        """Length of the trailing run of identical hashes.

        Example: hashes = [A, B, C, C, C] -> returns 3.
        """
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
        """How many full trailing cycles of *cycle_length* exist.

        A "cycle" is a subsequence of length `cycle_length` that repeats
        consecutively. Example with cycle_length=2:

            hashes = [X, A, B, A, B, A, B]
            -> trailing cycle is [A, B], repeated 3 times -> returns 3.

        If the trailing block doesn't match a repeating pattern, returns 0.
        """
        if cycle_length <= 0 or len(self.hashes) < cycle_length * 2:
            return 0

        hist: List[str] = list(self.hashes)
        pattern = hist[-cycle_length:]

        # The immediately preceding block must match the pattern.
        prev = hist[-2 * cycle_length : -cycle_length]
        if prev != pattern:
            return 0

        # Count how many full pattern blocks extend backward contiguously.
        repeats = 2
        idx = len(hist) - 2 * cycle_length
        while idx - cycle_length >= 0:
            block = hist[idx - cycle_length : idx]
            if block == pattern:
                repeats += 1
                idx -= cycle_length
            else:
                break
        return repeats


class SessionStore:
    """Thread-safe LRU store of SessionState objects.

    All public methods acquire an RLock. `get_or_create` promotes the
    accessed session to the end of the OrderedDict (most-recently-used),
    and new sessions trigger LRU eviction if the cap is exceeded.
    """

    def __init__(self, max_sessions: int | None = None) -> None:
        self._lock = threading.RLock()
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        self._max_sessions = max_sessions or settings.max_sessions
        self._started_at = time.time()

    def get_or_create(self, session_id: str) -> SessionState:
        """Return the existing state for *session_id* or create a new one."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = SessionState(session_id=session_id)
                self._sessions[session_id] = state
                self._evict_if_needed()
            else:
                self._sessions.move_to_end(session_id)
            return state

    def get(self, session_id: str) -> SessionState | None:
        """Return the existing state, or None if unknown. Does not create."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                self._sessions.move_to_end(session_id)
            return state

    def reset(self, session_id: str) -> bool:
        """Remove a session. Returns True if it existed."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def active_count(self) -> int:
        """Number of sessions currently retained."""
        with self._lock:
            return len(self._sessions)

    def uptime_seconds(self) -> float:
        """Seconds since the store was created."""
        return time.time() - self._started_at

    def _evict_if_needed(self) -> None:
        """Evict least-recently-used sessions until under the cap."""
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)


# Module-level singleton store used by the routes.
session_store = SessionStore()