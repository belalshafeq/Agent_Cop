"""HTTP-level protections: metrics, rate limiting, and body size enforcement."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from . import metrics
from .config import settings

logger = logging.getLogger("agenttrafficcop.middleware")


# ---------------------------------------------------------------------------
# Metrics middleware
# ---------------------------------------------------------------------------


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count and latency for every HTTP request.

    Runs before any other middleware so it captures the full lifecycle,
    including responses from the body-size limiter and error handlers.

    Records:
      * atc_requests_total{endpoint, method, status}  — counter
      * atc_request_duration_seconds{endpoint}        — histogram
    """

    async def dispatch(self, request: Request, call_next):
        endpoint = request.url.path
        method = request.method
        started = time.perf_counter()

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            # Unhandled exception — record it as a 500, then re-raise.
            duration = time.perf_counter() - started
            try:
                metrics.inc_request(endpoint, method, 500)
                metrics.observe_latency(endpoint, duration)
            except Exception:
                pass
            raise

        duration = time.perf_counter() - started
        try:
            metrics.inc_request(endpoint, method, status_code)
            metrics.observe_latency(endpoint, duration)
        except Exception:
            # Metrics must never break the request.
            pass

        return response


# ---------------------------------------------------------------------------
# Rate limiting — simple in-process sliding window
# ---------------------------------------------------------------------------


class _SlidingWindowLimiter:
    """A simple per-key sliding-window rate limiter.

    Tracks timestamps of recent calls per key. Allows up to
    `max_per_window` calls per `window_seconds`. Thread-safe enough for
    our use case (asyncio single-threaded event loop).

    NOT suitable for multi-replica deployments — for that, back it with
    Redis. This is the in-process version.
    """

    def __init__(self, max_per_window: int, window_seconds: int = 60, max_clients: int = 10000) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._max_clients = max_clients
        self._calls: Dict[str, Deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record a call for *key*.

        Returns:
            (allowed, retry_after_seconds)
        """
        now = time.monotonic()
        cutoff = now - self._window

        # Memory Leak Fix: If dictionary is too big, clear oldest entries.
        if len(self._calls) > self._max_clients:
            keys_to_remove = list(self._calls.keys())[:int(self._max_clients * 0.1)]
            for k in keys_to_remove:
                del self._calls[k]

        if key not in self._calls:
            self._calls[key] = deque()
            
        calls = self._calls[key]

        while calls and calls[0] < cutoff:
            calls.popleft()

        if len(calls) >= self._max:
            oldest = calls[0]
            retry_after = max(1, int(self._window - (now - oldest)) + 1)
            return False, retry_after

        calls.append(now)
        return True, 0


# Instantiate separate limiters for each tier
_limiters = {
    "free": _SlidingWindowLimiter(max_per_window=settings.tier_free_rate_limit),
    "pro": _SlidingWindowLimiter(max_per_window=settings.tier_pro_rate_limit),
    "team": _SlidingWindowLimiter(max_per_window=settings.tier_team_rate_limit),
}


def _client_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.
    
    On production servers (Render, Cloudflare, Nginx), request.client.host 
    is the proxy's IP. We MUST check the X-Forwarded-For header to get the 
    real user's IP address.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    
    if request.client is not None:
        return request.client.host
    return "unknown"


async def check_rate_limit(request: Request, key_fingerprint: str) -> None:
    """Enforce the rate limit based on the user's tier."""
    from .usage import get_tier_and_limit, _resolve_usage_key
    from .exceptions import RateLimitError

    tier, _ = get_tier_and_limit(key_fingerprint)
    
    # CRITICAL FIX: Rate limit by resolved user_id, not raw fingerprint.
    # This prevents users from generating new keys to bypass their limit.
    resolved_key = _resolve_usage_key(key_fingerprint)
    
    limiter = _limiters.get(tier, _limiters["free"])
    allowed, retry_after = limiter.check(resolved_key)
    
    if not allowed:
        logger.warning("rate_limit_exceeded tier=%s key=%s path=%s", tier, resolved_key, request.url.path)
        raise RateLimitError(
            message=f"Rate limit for {tier} tier exceeded.",
            retry_after=retry_after,
        )


# ---------------------------------------------------------------------------
# Body size enforcement
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured limit."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = None

            if length is not None and length > self.max_bytes:
                logger.warning(
                    "body_too_large path=%s declared_bytes=%d max_bytes=%d",
                    request.url.path,
                    length,
                    self.max_bytes,
                )
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={
                        "error": "REQUEST_TOO_LARGE",
                        "message": (
                            f"Request body exceeds the maximum of "
                            f"{self.max_bytes} bytes."
                        ),
                        "session_id": None,
                        "diagnostics": {
                            "max_bytes": self.max_bytes,
                            "declared_bytes": length,
                        },
                    },
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_middleware(app: FastAPI) -> None:
    """Wire HTTP-level protections into the app.

    Order matters: FastAPI/Starlette applies middleware in reverse order
    of `add_middleware` calls. The last one added runs first on the way
    in. We want:
      1. Metrics (outermost — sees everything, even rejected requests)
      2. Body size (rejects oversized before reading)
      3. Routes / handlers
    """
    # Added first → runs last
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_body_bytes,
    )
    # Added second → runs first
    app.add_middleware(MetricsMiddleware)

    logger.info(
        "middleware_installed max_body_bytes=%d rate_limit_per_minute=%d metrics_enabled=%s",
        settings.max_body_bytes,
        settings.rate_limit_per_minute,
        settings.metrics_enabled,
    )