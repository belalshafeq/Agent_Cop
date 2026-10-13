"""HTTP routes: /v1/route, /health, /v1/sessions/*, /metrics."""

from __future__ import annotations

import logging
import time
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__, metrics
from .audit import audit
from .circuit_breaker import evaluate
from .config import settings
from .hashing import payload_hash
from .middleware import check_rate_limit
from .schemas import (
    AgentMessage,
    CleanupRequest,
    CleanupResponse,
    HealthResponse,
    RouteApproval,
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
)
from .security import verify_api_key
from .state import session_store
from .state_backends import SessionSnapshot

logger = logging.getLogger("agenttrafficcop.routes")

router = APIRouter()


# ---------------------------------------------------------------------------
# Core governance endpoint
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Admin: session listing and inspection
# ---------------------------------------------------------------------------


def _snapshot_to_summary(snap: SessionSnapshot, now: float) -> SessionSummary:
    """Convert a SessionSnapshot into a SessionSummary for listing."""
    return SessionSummary(
        session_id=snap.session_id,
        step=snap.step,
        hash_count=len(snap.hashes),
        created_at=snap.created_at,
        last_seen=snap.last_seen,
        age_seconds=max(0.0, now - snap.created_at),
        idle_seconds=max(0.0, now - snap.last_seen),
    )


@router.get(
    "/v1/sessions",
    response_model=SessionListResponse,
    status_code=status.HTTP_200_OK,
    summary="List active sessions (admin).",
    responses={401: {"description": "Missing or invalid API key."}},
)
async def list_sessions(
    _key_fingerprint: Annotated[str, Depends(verify_api_key)],
    limit: int = Query(
        default=100,
        ge=1,
        le=10_000,
        description="Maximum number of sessions to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of sessions to skip (for pagination).",
    ),
    min_step: Optional[int] = Query(
        default=None,
        ge=0,
        description=(
            "If set, only return sessions whose step count is >= this value. "
            "Useful for finding potentially stuck sessions."
        ),
    ),
) -> SessionListResponse:
    """Return a summary of every active session.

    Pagination is applied after filtering. The `total` field reflects
    the number of sessions that matched the filter, not the number of
    sessions overall.
    """
    try:
        raw_snapshots = session_store.list_sessions()
    except Exception as e:
        logger.error("list_sessions_failed error=%s", e)
        raw_snapshots = []

    # Apply filter.
    if min_step is not None:
        raw_snapshots = [s for s in raw_snapshots if s.step >= min_step]

    # Sort by last_seen descending (most recently active first). This is
    # the most useful default for an operator scanning the list.
    raw_snapshots.sort(key=lambda s: s.last_seen, reverse=True)

    total = len(raw_snapshots)
    window = raw_snapshots[offset : offset + limit]

    now = time.time()
    summaries = [_snapshot_to_summary(s, now) for s in window]

    return SessionListResponse(
        total=total,
        returned=len(summaries),
        sessions=summaries,
    )


@router.get(
    "/v1/sessions/{session_id}",
    response_model=SessionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Inspect a single session (admin).",
    responses={
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Session not found."},
    },
)
async def get_session(
    session_id: str,
    _key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> SessionDetailResponse:
    """Return the full state of one session, including hash history.

    Returns 404 if the session doesn't exist. Never creates a session
    as a side effect — read-only endpoints must not mutate state.
    """
    try:
        snapshot = session_store.get(session_id)
    except Exception as e:
        logger.error("get_session_failed session_id=%s error=%s", session_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "SESSION_LOOKUP_FAILED"},
        )

    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "SESSION_NOT_FOUND",
                "message": f"Session '{session_id}' is not active.",
                "session_id": session_id,
            },
        )

    now = time.time()
    return SessionDetailResponse(
        session_id=snapshot.session_id,
        step=snapshot.step,
        hash_count=len(snapshot.hashes),
        hashes=list(snapshot.hashes),
        created_at=snapshot.created_at,
        last_seen=snapshot.last_seen,
        age_seconds=max(0.0, now - snapshot.created_at),
        idle_seconds=max(0.0, now - snapshot.last_seen),
    )

# ---------------------------------------------------------------------------
# Admin: bulk cleanup
# ---------------------------------------------------------------------------


@router.post(
    "/v1/admin/cleanup",
    response_model=CleanupResponse,
    status_code=status.HTTP_200_OK,
    summary="Expire sessions idle longer than a threshold (admin).",
    responses={401: {"description": "Missing or invalid API key."}},
)
async def cleanup_sessions(
    request: Request,
    body: CleanupRequest,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> CleanupResponse:
    """Remove sessions whose `last_seen` is older than `older_than_seconds`.

    The store is iterated, matching sessions are removed, and the counts
    of expired and remaining sessions are returned.
    """
    try:
        snapshots = session_store.list_sessions()
    except Exception as e:
        logger.error("cleanup_list_failed error=%s", e)
        snapshots = []

    now = time.time()
    cutoff = now - body.older_than_seconds

    expired = 0
    for snap in snapshots:
        if snap.last_seen < cutoff:
            try:
                if session_store.reset(snap.session_id):
                    expired += 1
                    try:
                        audit.log_reset(
                            session_id=snap.session_id,
                            removed=True,
                            api_key_fingerprint=key_fingerprint,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(
                    "cleanup_reset_failed session_id=%s error=%s",
                    snap.session_id,
                    e,
                )

    try:
        remaining = session_store.active_count()
    except Exception:
        remaining = 0

    try:
        metrics.set_active_sessions(remaining)
    except Exception:
        pass

    logger.info(
        "cleanup_completed expired=%d remaining=%d older_than_seconds=%d key=%s",
        expired,
        remaining,
        body.older_than_seconds,
        key_fingerprint,
    )

    return CleanupResponse(expired=expired, remaining=remaining)


# ---------------------------------------------------------------------------
# Existing: single-session reset
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


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