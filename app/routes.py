"""HTTP routes: /v1/route, /health, /v1/sessions/{id}."""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from . import __version__
from .circuit_breaker import evaluate
from .hashing import payload_hash
from .schemas import AgentMessage, HealthResponse, RouteApproval
from .security import verify_api_key
from .state import session_store

logger = logging.getLogger("agenttrafficcop.routes")

router = APIRouter()


@router.post(
    "/v1/route",
    response_model=RouteApproval,
    status_code=status.HTTP_200_OK,
    summary="Evaluate and approve an inter-agent message.",
    responses={
        401: {"description": "Missing or invalid API key."},
        422: {
            "description": (
                "Schema validation failed or the circuit breaker tripped."
            )
        },
    },
)
async def route_message(
    message: AgentMessage,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> RouteApproval:
    """Governance checkpoint for a single agent-to-agent handoff."""
    started = time.perf_counter()

    digest = payload_hash(message.payload)
    snapshot = session_store.get_or_create(message.session_id)
    new_snapshot = evaluate(snapshot, digest)

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    logger.info(
        "message_approved session_id=%s sender=%s recipient=%s "
        "intent=%s step=%d hash=%s elapsed_ms=%.3f key=%s",
        message.session_id,
        message.sender,
        message.recipient,
        message.intent,
        new_snapshot.step,
        digest,
        elapsed_ms,
        key_fingerprint,
    )

    return RouteApproval(
        session_id=message.session_id,
        step=new_snapshot.step,
        payload_hash=digest,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="System health check.",
)
async def health() -> HealthResponse:
    """Liveness/readiness probe. Intentionally unauthenticated.

    Never raises. If the session backend is unreachable, we still
    return 200 with active_sessions=0 and log the failure — because
    a health check that 500s is not useful to an orchestrator.
    """
    try:
        active = session_store.active_count()
    except Exception as e:
        logger.error("health_active_count_failed error=%s", e)
        active = 0

    try:
        uptime = session_store.uptime_seconds()
    except Exception:
        uptime = 0.0

    return HealthResponse(
        version=__version__,
        active_sessions=active,
        uptime_seconds=round(uptime, 3),
    )


@router.delete(
    "/v1/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    summary="Reset a session (admin utility).",
    responses={401: {"description": "Missing or invalid API key."}},
)
async def reset_session(
    session_id: str,
    _key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> JSONResponse:
    """Remove a session from the state engine."""
    removed = session_store.reset(session_id)
    logger.info(
        "session_reset session_id=%s removed=%s",
        session_id,
        removed,
    )
    return JSONResponse(
        status_code=200,
        content={"session_id": session_id, "removed": removed},
    )