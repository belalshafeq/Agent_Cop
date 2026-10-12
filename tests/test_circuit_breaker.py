"""Tests for app.circuit_breaker — the core loop detection logic.

Assumptions:
  * Each test starts with a FRESH in-memory session store.
  * Thresholds come from app.config.settings defaults:
        max_steps = 25
        max_repeated_hashes = 3
        max_cycle_length = 2
        max_cycle_repeats = 3
  * We drive the breaker directly (no HTTP) for speed.
"""

from __future__ import annotations

import pytest

from app.circuit_breaker import evaluate
from app.config import settings
from app.exceptions import (
    CyclicPayloadLoop,
    RepeatedPayloadLoop,
    StepLimitExceeded,
)
from app.state import session_store
from app.state_backends import SessionSnapshot


# --- Helpers -----------------------------------------------------------------


def fresh_snapshot(session_id: str) -> SessionSnapshot:
    """Get-or-create the snapshot for a session (fresh store per test)."""
    return session_store.get_or_create(session_id)


def hash_of(n: int) -> str:
    """Deterministic 64-char hex string for tests."""
    return f"{n:064x}"


# --- Happy path --------------------------------------------------------------


class TestHappyPath:
    def test_first_message_approves(self):
        s = fresh_snapshot("happy-1")
        result = evaluate(s, hash_of(1))
        assert result.step == 1
        assert result.hashes == [hash_of(1)]

    def test_multiple_distinct_messages_approve(self):
        s = fresh_snapshot("happy-2")
        for i in range(1, 5):
            s = evaluate(s, hash_of(i))
        assert s.step == 4
        assert s.hashes == [hash_of(i) for i in range(1, 5)]

    def test_distinct_payloads_never_trip(self):
        s = fresh_snapshot("happy-3")
        for i in range(1, 11):
            s = evaluate(s, hash_of(i))
        assert s.step == 10


# --- Step limit --------------------------------------------------------------


class TestStepLimit:
    def test_trips_at_max_steps(self):
        """Reaching max_steps is fine; the NEXT call trips."""
        s = fresh_snapshot("step-1")
        for i in range(settings.max_steps):
            s = evaluate(s, hash_of(i))
        assert s.step == settings.max_steps

        with pytest.raises(StepLimitExceeded) as exc_info:
            evaluate(s, hash_of(settings.max_steps + 1))

        err = exc_info.value
        assert err.error_code == "STEP_LIMIT_EXCEEDED"
        assert err.session_id == "step-1"
        assert err.diagnostics["max_steps"] == settings.max_steps

    def test_step_limit_checked_before_recording(self):
        s = fresh_snapshot("step-2")
        for i in range(settings.max_steps):
            s = evaluate(s, hash_of(i))

        with pytest.raises(StepLimitExceeded):
            evaluate(s, hash_of(999))

        current = session_store.get_or_create("step-2")
        assert current.step == settings.max_steps


# --- Repeated payloads -------------------------------------------------------


class TestRepeatedPayloads:
    def test_trips_at_threshold(self):
        s = fresh_snapshot("repeat-1")
        same = hash_of(42)

        # Approve (threshold - 1) identical hashes.
        for _ in range(settings.max_repeated_hashes - 1):
            s = evaluate(s, same)
        assert s.step == settings.max_repeated_hashes - 1

        # The Nth identical hash trips.
        with pytest.raises(RepeatedPayloadLoop) as exc_info:
            evaluate(s, same)

        err = exc_info.value
        assert err.error_code == "REPEATED_PAYLOAD_LOOP"
        assert err.session_id == "repeat-1"
        assert err.diagnostics["consecutive_repeats"] == settings.max_repeated_hashes
        assert err.diagnostics["repeated_hash"] == same
        assert err.diagnostics["step"] == settings.max_repeated_hashes

    def test_different_hash_after_repeats_resets_counter(self):
        """A,A,B: the trailing run of A ends when B arrives."""
        s = fresh_snapshot("repeat-2")
        a = hash_of(42)
        b = hash_of(43)

        for _ in range(settings.max_repeated_hashes - 1):
            s = evaluate(s, a)

        # B is different — resets the counter.
        s = evaluate(s, b)

        # A again — should be approved (counter starts fresh).
        s = evaluate(s, a)
        assert s.step == settings.max_repeated_hashes + 1


# --- Cyclic payloads ---------------------------------------------------------


class TestCyclicPayloads:
    def test_two_way_cycle_trips(self):
        """A,B,A,B,... enough times → trip."""
        s = fresh_snapshot("cycle-1")
        a = hash_of(1)
        b = hash_of(2)

        # Send (max_cycle_repeats - 1) complete cycles.
        for _ in range(settings.max_cycle_repeats - 1):
            s = evaluate(s, a)
            s = evaluate(s, b)

        # Sending `a` first is fine (only 2 full cycles so far).
        s = evaluate(s, a)

        # The next `b` completes the 3rd full cycle → trip.
        with pytest.raises(CyclicPayloadLoop) as exc_info:
            evaluate(s, b)

        err = exc_info.value
        assert err.error_code == "CYCLIC_PAYLOAD_LOOP"
        assert err.session_id == "cycle-1"
        assert err.diagnostics["cycle_length"] == 2
        assert err.diagnostics["cycle_repeats"] == settings.max_cycle_repeats

    def test_broken_cycle_does_not_trip(self):
        """A,B,A,B then C — C breaks the pattern, no trip."""
        s = fresh_snapshot("cycle-2")
        a = hash_of(1)
        b = hash_of(2)
        c = hash_of(3)

        # Only 2 cycles of A,B (below the threshold of 3).
        for _ in range(settings.max_cycle_repeats - 1):
            s = evaluate(s, a)
            s = evaluate(s, b)

        # Break with C — no trip should occur.
        s = evaluate(s, c)
        assert s.step > 0

    def test_three_way_cycle_trips(self):
        if settings.max_cycle_length < 3:
            pytest.skip("max_cycle_length < 3; 3-way cycles not detected")

        s = fresh_snapshot("cycle-3")
        a, b, c = hash_of(1), hash_of(2), hash_of(3)

        for _ in range(settings.max_cycle_repeats - 1):
            s = evaluate(s, a)
            s = evaluate(s, b)
            s = evaluate(s, c)

        s = evaluate(s, a)
        s = evaluate(s, b)
        with pytest.raises(CyclicPayloadLoop):
            evaluate(s, c)


# --- Check ordering ----------------------------------------------------------


class TestCheckInteractions:
    def test_repeat_check_fires_before_cycle_check(self):
        """A,A,A → RepeatedPayloadLoop (not CyclicPayloadLoop)."""
        s = fresh_snapshot("interact-1")
        a = hash_of(1)
        for _ in range(settings.max_repeated_hashes - 1):
            s = evaluate(s, a)

        with pytest.raises(RepeatedPayloadLoop):
            evaluate(s, a)

    def test_step_limit_fires_before_repeat_check(self):
        """At max_steps, StepLimitExceeded wins even if hash repeats."""
        s = fresh_snapshot("interact-2")
        for i in range(settings.max_steps):
            s = evaluate(s, hash_of(i))

        # A repeat of the last hash would normally trip RepeatedPayloadLoop,
        # but StepLimitExceeded fires first.
        with pytest.raises(StepLimitExceeded):
            evaluate(s, hash_of(settings.max_steps - 1))


# --- Snapshot isolation ------------------------------------------------------


class TestSnapshotIsolation:
    def test_evaluate_returns_a_new_snapshot(self):
        s = fresh_snapshot("iso-1")
        assert s.step == 0

        result = evaluate(s, hash_of(1))
        assert result is not s
        # The input snapshot's step is unchanged (it's immutable anyway).
        assert s.step == 0
        assert result.step == 1

    def test_snapshot_hashes_is_a_copy(self):
        """The list returned in a snapshot is not the store's deque."""
        s = fresh_snapshot("iso-2")
        result = evaluate(s, hash_of(1))
        original_len = len(result.hashes)

        # Mutating the returned list must not affect the store.
        result.hashes.append("garbage")

        current = session_store.get_or_create("iso-2")
        assert len(current.hashes) == original_len