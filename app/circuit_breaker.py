"""Circuit breaker logic for step limits and loop detection.

Given a session's state and a new payload hash, this module decides
whether to allow the step or raise a governance exception. The three
checks run in a fixed order so the diagnostic that reaches the caller
is the most specific one available.

All thresholds are read from `settings` (env-driven). Nothing here
trusts user input directly — the payload and session_id have already
been validated by Pydantic in `schemas.py`.
"""

from __future__ import annotations

import logging

from .config import settings
from .exceptions import (
    CyclicPayloadLoop,
    RepeatedPayloadLoop,
    StepLimitExceeded,
)
from .state import SessionState

logger = logging.getLogger("agenttrafficcop.breaker")


def evaluate(state: SessionState, payload_hash: str) -> int:
    """Record the payload hash and enforce all governance thresholds.

    Returns the new step count on success.

    Raises:
        StepLimitExceeded: if the session has hit or exceeded `max_steps`.
        RepeatedPayloadLoop: if the same hash repeats consecutively.
        CyclicPayloadLoop: if a short repeating cycle is detected.
    """
    # --- Check 1: hard step cap ------------------------------------------
    # Checked BEFORE recording so `max_steps` is a true ceiling: the
    # (max_steps + 1)-th message never gets counted.
    if state.step >= settings.max_steps:
        logger.warning(
            "step_limit_exceeded session_id=%s step=%d max=%d",
            state.session_id,
            state.step,
            settings.max_steps,
        )
        raise StepLimitExceeded(
            message=(
                f"Session '{state.session_id}' exceeded the maximum of "
                f"{settings.max_steps} steps."
            ),
            session_id=state.session_id,
            diagnostics={
                "step": state.step,
                "max_steps": settings.max_steps,
                "recent_hashes": list(state.hashes),
            },
        )

    # Record the new step. From here on, `state.step` reflects the
    # just-recorded message.
    step = state.record(payload_hash)

    # --- Check 2: consecutive identical hashes ---------------------------
    repeats = state.consecutive_repeats()
    if repeats >= settings.max_repeated_hashes:
        logger.warning(
            "repeated_payload_loop session_id=%s step=%d repeats=%d",
            state.session_id,
            step,
            repeats,
        )
        raise RepeatedPayloadLoop(
            message=(
                f"Session '{state.session_id}' produced identical payload "
                f"hash {repeats} times consecutively."
            ),
            session_id=state.session_id,
            diagnostics={
                "step": step,
                "repeated_hash": payload_hash,
                "consecutive_repeats": repeats,
                "max_repeated_hashes": settings.max_repeated_hashes,
            },
        )

    # --- Check 3: cyclic (ping-pong) patterns ----------------------------
    # Cycle length 1 is already covered by the consecutive check above,
    # so we scan from 2 up to `max_cycle_length`.
    for cycle_len in range(2, settings.max_cycle_length + 1):
        repeats = state.cycle_repeats(cycle_len)
        if repeats >= settings.max_cycle_repeats:
            logger.warning(
                "cyclic_payload_loop session_id=%s step=%d cycle_len=%d repeats=%d",
                state.session_id,
                step,
                cycle_len,
                repeats,
            )
            raise CyclicPayloadLoop(
                message=(
                    f"Session '{state.session_id}' entered a repeating "
                    f"cycle of length {cycle_len} ({repeats} times)."
                ),
                session_id=state.session_id,
                diagnostics={
                    "step": step,
                    "cycle_length": cycle_len,
                    "cycle_repeats": repeats,
                    "max_cycle_repeats": settings.max_cycle_repeats,
                    "recent_hashes": list(state.hashes),
                },
            )

    # All checks passed.
    return step