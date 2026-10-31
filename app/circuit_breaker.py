"""Circuit breaker logic for step limits and loop detection."""

from __future__ import annotations

import logging
from typing import Optional

from . import metrics
from .audit import audit
from .config import settings
from .exceptions import (
    CyclicPayloadLoop,
    RepeatedPayloadLoop,
    StepLimitExceeded,
)
from .state import session_store
from .state_backends import SessionSnapshot

logger = logging.getLogger("agenttrafficcop.breaker")


def evaluate(
    snapshot: SessionSnapshot,
    payload_hash: str,
    *,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    api_key_fingerprint: Optional[str] = None,
) -> SessionSnapshot:
    """Record the payload hash and enforce all governance thresholds."""

    # --- Check 1: hard step cap ------------------------------------------
    if snapshot.step >= settings.max_steps:
        logger.warning(
            "step_limit_exceeded session_id=%s step=%d max=%d",
            snapshot.session_id, snapshot.step, settings.max_steps,
        )
        try:
            metrics.inc_trip("step_limit")
        except Exception:
            pass
        try:
            audit.log_trip(
                session_id=snapshot.session_id,
                error_code="STEP_LIMIT_EXCEEDED",
                step=snapshot.step,
                sender=sender,
                recipient=recipient,
                diagnostics={
                    "step": snapshot.step,
                    "max_steps": settings.max_steps,
                },
                api_key_fingerprint=api_key_fingerprint,
            )
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

    # FIX 4: Build a temporary snapshot to test loops BEFORE committing the step.
    # This ensures a tripped loop doesn't eat up the user's step budget.
    new_hashes = snapshot.hashes + [payload_hash]
    temp_snapshot = SessionSnapshot(
        session_id=snapshot.session_id,
        step=snapshot.step + 1,
        hashes=new_hashes,
        created_at=snapshot.created_at,
        last_seen=snapshot.last_seen,
    )

    # --- Check 2: consecutive identical hashes ---------------------------
    repeats = temp_snapshot.consecutive_repeats()
    if repeats >= settings.max_repeated_hashes:
        logger.warning(
            "repeated_payload_loop session_id=%s step=%d repeats=%d",
            temp_snapshot.session_id, temp_snapshot.step, repeats,
        )
        try:
            metrics.inc_trip("repeated_payload")
        except Exception:
            pass
        try:
            audit.log_trip(
                session_id=temp_snapshot.session_id,
                error_code="REPEATED_PAYLOAD_LOOP",
                step=temp_snapshot.step,
                sender=sender,
                recipient=recipient,
                diagnostics={
                    "repeated_hash": payload_hash,
                    "consecutive_repeats": repeats,
                    "max_repeated_hashes": settings.max_repeated_hashes,
                },
                api_key_fingerprint=api_key_fingerprint,
            )
        except Exception:
            pass
        raise RepeatedPayloadLoop(
            message=(
                f"Session '{temp_snapshot.session_id}' produced identical "
                f"payload hash {repeats} times consecutively."
            ),
            session_id=temp_snapshot.session_id,
            diagnostics={
                "step": temp_snapshot.step,
                "repeated_hash": payload_hash,
                "consecutive_repeats": repeats,
                "max_repeated_hashes": settings.max_repeated_hashes,
            },
        )

    # --- Check 3: cyclic (ping-pong) patterns ----------------------------
    for cycle_len in range(2, settings.max_cycle_length + 1):
        repeats = temp_snapshot.cycle_repeats(cycle_len)
        if repeats >= settings.max_cycle_repeats:
            logger.warning(
                "cyclic_payload_loop session_id=%s step=%d cycle_len=%d repeats=%d",
                temp_snapshot.session_id, temp_snapshot.step, cycle_len, repeats,
            )
            try:
                metrics.inc_trip("cyclic_payload")
            except Exception:
                pass
            try:
                audit.log_trip(
                    session_id=temp_snapshot.session_id,
                    error_code="CYCLIC_PAYLOAD_LOOP",
                    step=temp_snapshot.step,
                    sender=sender,
                    recipient=recipient,
                    diagnostics={
                        "cycle_length": cycle_len,
                        "cycle_repeats": repeats,
                        "max_cycle_repeats": settings.max_cycle_repeats,
                    },
                    api_key_fingerprint=api_key_fingerprint,
                )
            except Exception:
                pass
            raise CyclicPayloadLoop(
                message=(
                    f"Session '{temp_snapshot.session_id}' entered a "
                    f"repeating cycle of length {cycle_len} ({repeats} times)."
                ),
                session_id=temp_snapshot.session_id,
                diagnostics={
                    "step": temp_snapshot.step,
                    "cycle_length": cycle_len,
                    "cycle_repeats": repeats,
                    "max_cycle_repeats": settings.max_cycle_repeats,
                    "recent_hashes": temp_snapshot.hashes,
                },
            )

    # If all checks passed, we officially record the step.
    new_snapshot = session_store.record(snapshot.session_id, payload_hash)
    return new_snapshot