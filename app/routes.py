"""HTTP routes: /v1/route, /health, /v1/sessions/*, /v1/usage, /metrics."""

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
from .exceptions import AgentLimitExceeded, TierLimitExceeded
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
    UsageResponse,
)
from .security import _fingerprint, verify_api_key
from .state import session_store
from .state_backends import SessionSnapshot
from .usage import (
    check_and_add_agents_atomic,
    get_agents_set,
    get_tier_and_limit,
    is_over_limit,
    record_message,
    snapshot,
)

logger = logging.getLogger("agenttrafficcop.routes")

router = APIRouter()


# FIX: Admin authorization helper.
# If ATC_ADMIN_API_KEYS is set, only those keys can hit admin endpoints.
# If empty, we fall back to requiring the `team` tier.
def _require_admin(key_fingerprint: str) -> None:
    admin_fps = {_fingerprint(k) for k in settings.admin_api_keys}
    if admin_fps:
        if key_fingerprint not in admin_fps:
            logger.warning(
                "admin_access_denied fingerprint=%s path=admin",
                key_fingerprint,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "ADMIN_REQUIRED",
                    "message": "This endpoint requires an admin API key.",
                },
            )
        return

    tier, _ = get_tier_and_limit(key_fingerprint)
    if tier != "team":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "ADMIN_REQUIRED",
                "message": "This endpoint requires an admin-tier key.",
            },
        )


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
        402: {"description": "Agent limit exceeded. Upgrade required."},
        413: {"description": "Request body too large."},
        422: {"description": "Schema validation failed or breaker tripped."},
        429: {"description": "Rate limit or tier limit exceeded."},
    },
)
async def route_message(
    request: Request,
    message: AgentMessage,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> RouteApproval:
    """Governance checkpoint for a single agent-to-agent handoff."""
    # 1. Per-minute rate limit.
    await check_rate_limit(request, key_fingerprint)

    # 2. Monthly tier limit.
    over, tier, used, limit = is_over_limit(key_fingerprint)
    if over:
        from .usage import next_month_start_utc

        reset_at = next_month_start_utc()
        retry_after = _seconds_until_next_month()
        logger.warning(
            "tier_limit_exceeded tier=%s used=%d limit=%d fingerprint=%s",
            tier, used, limit, key_fingerprint,
        )
        try:
            audit.log_rate_limited(
                path=str(request.url.path),
                retry_after=retry_after,
            )
        except Exception:
            pass
        raise TierLimitExceeded(
            message=(
                f"Tier '{tier}' limit of {limit} messages per month reached. "
                f"Resets on {reset_at}."
            ),
            tier=tier,
            used=used,
            limit=limit,
            reset_at=reset_at,
            retry_after=retry_after,
            session_id=message.session_id,
        )

    # 3. Circuit breaker (may consume a step before raising).
    started = time.perf_counter()

    digest = payload_hash(message.payload)
    snap = session_store.get_or_create(message.session_id)
    new_snapshot = evaluate(
        snap,
        digest,
        sender=message.sender,
        recipient=message.recipient,
        api_key_fingerprint=key_fingerprint,
    )

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    # 4. Agent limit — atomic check-and-add.
    # FIX: Prevents TOCTOU race where concurrent requests bypass the limit.
    agent_limit = settings.agent_limit_for_tier(tier)
    requested_agents = {message.sender, message.recipient}
    allowed, newly_added = check_and_add_agents_atomic(
        key_fingerprint, requested_agents, agent_limit,
    )
    if not allowed:
        current_agents = get_agents_set(key_fingerprint)
        logger.warning(
            "agent_limit_exceeded tier=%s current=%d limit=%d key=%s",
            tier, len(current_agents), agent_limit, key_fingerprint,
        )
        raise AgentLimitExceeded(
            message=(
                f"Adding this handoff would introduce agents beyond your "
                f"{tier} tier limit of {agent_limit} unique agents per month. "
                f"Upgrade to protect more agents."
            ),
            tier=tier,
            current_agents=len(current_agents),
            limit=agent_limit,
            attempted_new=sorted(newly_added),
            session_id=message.session_id,
        )

    # 5. Record message usage only after all checks pass.
    new_usage = record_message(key_fingerprint)

    metrics.inc_approved()
    try:
        metrics.set_active_sessions(session_store.active_count())
    except Exception:
        pass

    logger.info(
        "message_approved session_id=%s sender=%s recipient=%s "
        "intent=%s step=%d hash=%s elapsed_ms=%.3f key=%s "
        "tier=%s usage_this_month=%d",
        message.session_id,
        message.sender,
        message.recipient,
        message.intent,
        new_snapshot.step,
        digest,
        elapsed_ms,
        key_fingerprint,
        tier,
        new_usage,
    )

    return RouteApproval(
        session_id=message.session_id,
        step=new_snapshot.step,
        payload_hash=digest,
    )


def _seconds_until_next_month() -> int:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    next_start = datetime(year, month, 1, tzinfo=timezone.utc)
    delta = (next_start - now).total_seconds()
    return max(1, int(delta))


# ---------------------------------------------------------------------------
# Usage endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/v1/usage",
    response_model=UsageResponse,
    status_code=status.HTTP_200_OK,
    summary="Show this API key's usage and tier.",
    responses={401: {"description": "Missing or invalid API key."}},
)
async def get_my_usage(
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> UsageResponse:
    snap = snapshot(key_fingerprint)
    return UsageResponse(**snap.to_dict())


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
# Admin: session listing and inspection (now gated)
# ---------------------------------------------------------------------------


def _snapshot_to_summary(s: SessionSnapshot, now: float) -> SessionSummary:
    return SessionSummary(
        session_id=s.session_id,
        step=s.step,
        hash_count=len(s.hashes),
        created_at=s.created_at,
        last_seen=s.last_seen,
        age_seconds=max(0.0, now - s.created_at),
        idle_seconds=max(0.0, now - s.last_seen),
    )


@router.get(
    "/v1/sessions",
    response_model=SessionListResponse,
    status_code=status.HTTP_200_OK,
    summary="List active sessions (admin).",
    responses={
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Admin access required."},
    },
)
async def list_sessions(
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
    limit: int = Query(default=100, ge=1, le=10_000),
    offset: int = Query(default=0, ge=0),
    min_step: Optional[int] = Query(default=None, ge=0),
) -> SessionListResponse:
    _require_admin(key_fingerprint)

    try:
        raw = session_store.list_sessions()
    except Exception as e:
        logger.error("list_sessions_failed error=%s", e)
        raw = []

    if min_step is not None:
        raw = [s for s in raw if s.step >= min_step]

    raw.sort(key=lambda s: s.last_seen, reverse=True)

    total = len(raw)
    window = raw[offset : offset + limit]
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
        403: {"description": "Admin access required."},
        404: {"description": "Session not found."},
    },
)
async def get_session(
    session_id: str,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> SessionDetailResponse:
    _require_admin(key_fingerprint)

    try:
        snap = session_store.get(session_id)
    except Exception as e:
        logger.error("get_session_failed session_id=%s error=%s", session_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "SESSION_LOOKUP_FAILED"},
        )

    if snap is None:
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
        session_id=snap.session_id,
        step=snap.step,
        hash_count=len(snap.hashes),
        hashes=list(snap.hashes),
        created_at=snap.created_at,
        last_seen=snap.last_seen,
        age_seconds=max(0.0, now - snap.created_at),
        idle_seconds=max(0.0, now - snap.last_seen),
    )


# ---------------------------------------------------------------------------
# Admin: bulk cleanup (now gated)
# ---------------------------------------------------------------------------


@router.post(
    "/v1/admin/cleanup",
    response_model=CleanupResponse,
    status_code=status.HTTP_200_OK,
    summary="Expire idle sessions (admin).",
    responses={
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Admin access required."},
    },
)
async def cleanup_sessions(
    request: Request,
    body: CleanupRequest,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> CleanupResponse:
    _require_admin(key_fingerprint)

    try:
        snapshots = session_store.list_sessions()
    except Exception as e:
        logger.error("cleanup_list_failed error=%s", e)
        snapshots = []

    now = time.time()
    cutoff = now - body.older_than_seconds

    expired = 0
    for s in snapshots:
        if s.last_seen < cutoff:
            try:
                if session_store.reset(s.session_id):
                    expired += 1
                    try:
                        audit.log_reset(
                            session_id=s.session_id,
                            removed=True,
                            api_key_fingerprint=key_fingerprint,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(
                    "cleanup_reset_failed session_id=%s error=%s",
                    s.session_id, e,
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
        expired, remaining, body.older_than_seconds, key_fingerprint,
    )

    return CleanupResponse(expired=expired, remaining=remaining)


# ---------------------------------------------------------------------------
# Reset single session (now gated)
# ---------------------------------------------------------------------------


@router.delete(
    "/v1/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    summary="Reset a session (admin).",
    responses={
        401: {"description": "Missing or invalid API key."},
        403: {"description": "Admin access required."},
    },
)
async def reset_session(
    session_id: str,
    key_fingerprint: Annotated[str, Depends(verify_api_key)],
) -> JSONResponse:
    _require_admin(key_fingerprint)

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

    logger.info("session_reset session_id=%s removed=%s", session_id, removed)
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
    if not settings.metrics_enabled:
        return PlainTextResponse(
            "# metrics disabled\n",
            status_code=200,
            media_type="text/plain; version=0.0.4",
        )
    body = metrics.render()
    return Response(content=body, media_type="text/plain; version=0.0.4")