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
    """Governance checkpoint for a single agent-to-agent handoff.

    Flow:
        1. FastAPI + Pydantic validate the incoming message.
        2. The auth dependency has already verified the Bearer token.
        3. Compute a deterministic SHA-256 hash of the payload.
        4. Retrieve or create the session state.
        5. Run the circuit breaker (step + loop detection).
        6. Emit a structured approval log and return the response.

    Any governance violation raises a `TrafficCopError` subclass, which
    the exception handlers convert into a structured HTTP 422 response.
    """
    started = time.perf_counter()

    digest = payload_hash(message.payload)
    state = session_store.get_or_create(message.session_id)
    step = evaluate(state, digest)

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    logger.info(
        "message_approved session_id=%s sender=%s recipient=%s "
        "intent=%s step=%d hash=%s elapsed_ms=%.3f key=%s",
        message.session_id,
        message.sender,
        message.recipient,
        message.intent,
        step,
        digest,
        elapsed_ms,
        key_fingerprint,
    )

    return RouteApproval(
        session_id=message.session_id,
        step=step,
        payload_hash=digest,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="System health check.",
)
async def health() -> HealthResponse:
    """Liveness/readiness probe.

    Intentionally unauthenticated so infrastructure probes can reach it.
    Leaks only session count and uptime — no secrets, no payloads.
    """
    return HealthResponse(
        version=__version__,
        active_sessions=session_store.active_count(),
        uptime_seconds=round(session_store.uptime_seconds(), 3),
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
    """Remove a session from the state engine.

    Useful for tests, for operators ending a run explicitly, or for
    clearing a stuck session without restarting the process.
    """
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