"""Circuit breaker logic for step limits and loop detection.

Given a session's current snapshot and the session store, this module
decides whether to allow the step or raise a governance exception.

The three checks run in a fixed order so the diagnostic that reaches
the caller is the most specific one available.
"""

from __future__ import annotations

import logging

from .config import settings
from .exceptions import (
    CyclicPayloadLoop,
    RepeatedPayloadLoop,
    StepLimitExceeded,
)
from .state import session_store
from .state_backends import SessionSnapshot

logger = logging.getLogger("agenttrafficcop.breaker")


def evaluate(snapshot: SessionSnapshot, payload_hash: str) -> SessionSnapshot:
    """Record the payload hash and enforce all governance thresholds.

    Args:
        snapshot:     The session's current state *before* this message.
        payload_hash: SHA-256 of the incoming payload.

    Returns:
        The new session snapshot *after* recording the step.

    Raises:
        StepLimitExceeded: if the session has hit or exceeded max_steps.
        RepeatedPayloadLoop: if the same hash repeats consecutively.
        CyclicPayloadLoop: if a short repeating cycle is detected.
    """
    # --- Check 1: hard step cap ------------------------------------------
    if snapshot.step >= settings.max_steps:
        logger.warning(
            "step_limit_exceeded session_id=%s step=%d max=%d",
            snapshot.session_id,
            snapshot.step,
            settings.max_steps,
        )
        raise StepLimitExceeded(
            message=(
                f"Session '{snapshot.session_id}' exceeded the maximum of "
                f"{settings.max_steps} steps."
            ),
            session_id=snapshot.session_id,
            diagnostics={
                "step": snapshot.step,
                "max_steps": settings.max_steps,
                "recent_hashes": snapshot.hashes,
            },
        )

    # Record the step via the backend. Returns a fresh snapshot.
    new_snapshot = session_store.record(snapshot.session_id, payload_hash)

    # --- Check 2: consecutive identical hashes ---------------------------
    repeats = new_snapshot.consecutive_repeats()
    if repeats >= settings.max_repeated_hashes:
        logger.warning(
            "repeated_payload_loop session_id=%s step=%d repeats=%d",
            new_snapshot.session_id,
            new_snapshot.step,
            repeats,
        )
        raise RepeatedPayloadLoop(
            message=(
                f"Session '{new_snapshot.session_id}' produced identical "
                f"payload hash {repeats} times consecutively."
            ),
            session_id=new_snapshot.session_id,
            diagnostics={
                "step": new_snapshot.step,
                "repeated_hash": payload_hash,
                "consecutive_repeats": repeats,
                "max_repeated_hashes": settings.max_repeated_hashes,
            },
        )

    # --- Check 3: cyclic (ping-pong) patterns ----------------------------
    for cycle_len in range(2, settings.max_cycle_length + 1):
        repeats = new_snapshot.cycle_repeats(cycle_len)
        if repeats >= settings.max_cycle_repeats:
            logger.warning(
                "cyclic_payload_loop session_id=%s step=%d cycle_len=%d repeats=%d",
                new_snapshot.session_id,
                new_snapshot.step,
                cycle_len,
                repeats,
            )
            raise CyclicPayloadLoop(
                message=(
                    f"Session '{new_snapshot.session_id}' entered a "
                    f"repeating cycle of length {cycle_len} ({repeats} times)."
                ),
                session_id=new_snapshot.session_id,
                diagnostics={
                    "step": new_snapshot.step,
                    "cycle_length": cycle_len,
                    "cycle_repeats": repeats,
                    "max_cycle_repeats": settings.max_cycle_repeats,
                    "recent_hashes": new_snapshot.hashes,
                },
            )

    return new_snapshot