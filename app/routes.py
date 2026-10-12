"""HTTP routes: /v1/route, /health, /v1/sessions/{id}, /metrics."""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__, metrics
from .audit import audit
from .circuit_breaker import evaluate
from .config import settings
from .hashing import payload_hash
from .middleware import check_rate_limit
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
        413: {"description": "Request body too large."},
        422: {"description": "Schema validation failed or breaker tripped."},
        429: {"description": "Rate limit exceeded."},
    },
)
async def route_message(
    request: Request,
    message: AgentMessage,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> RouteApproval:
    """Governance checkpoint for a single agent-to-agent handoff."""
    await check_rate_limit(request)

    started = time.perf_counter()

    digest = payload_hash(message.payload)
    snapshot = session_store.get_or_create(message.session_id)
    new_snapshot = evaluate(
        snapshot,
        digest,
        sender=message.sender,
        recipient=message.recipient,
        api_key_fingerprint=key_fingerprint,
    )

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    metrics.inc_approved()
    try:
        metrics.set_active_sessions(session_store.active_count())
    except Exception:
        pass

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
    """Liveness/readiness probe. Intentionally unauthenticated."""
    try:
        active = session_store.active_count()
    except Exception as e:
        logger.error("health_active_count_failed error=%s", e)
        active = 0

    try:
        uptime = session_store.uptime_seconds()
    except Exception:
        uptime = 0.0

    try:
        metrics.set_active_sessions(active)
    except Exception:
        pass

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
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> JSONResponse:
    """Remove a session from the state engine."""
    removed = session_store.reset(session_id)
    try:
        metrics.set_active_sessions(session_store.active_count())
    except Exception:
        pass

    try:
        audit.log_reset(
            session_id=session_id,
            removed=removed,
            api_key_fingerprint=key_fingerprint,
        )
    except Exception:
        pass

    logger.info(
        "session_reset session_id=%s removed=%s",
        session_id,
        removed,
    )
    return JSONResponse(
        status_code=200,
        content={"session_id": session_id, "removed": removed},
    )


@router.get(
    settings.metrics_path,
    include_in_schema=False,
    summary="Prometheus metrics endpoint.",
)
async def metrics_endpoint() -> Response:
    """Return all metrics in Prometheus text exposition format."""
    if not settings.metrics_enabled:
        return PlainTextResponse(
            "# metrics disabled\n",
            status_code=200,
            media_type="text/plain; version=0.0.4",
        )

    body = metrics.render()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4",
    )