"""Circuit breaker logic for step limits and loop detection."""

from __future__ import annotations

import logging

from . import metrics
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
    """Record the payload hash and enforce all governance thresholds."""
    # --- Check 1: hard step cap ------------------------------------------
    if snapshot.step >= settings.max_steps:
        logger.warning(
            "step_limit_exceeded session_id=%s step=%d max=%d",
            snapshot.session_id,
            snapshot.step,
            settings.max_steps,
        )
        try:
            metrics.inc_trip("step_limit")
        except Exception:
            pass
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
        try:
            metrics.inc_trip("repeated_payload")
        except Exception:
            pass
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
            try:
                metrics.inc_trip("cyclic_payload")
            except Exception:
                pass
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