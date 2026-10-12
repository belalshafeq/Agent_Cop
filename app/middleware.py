"""HTTP-level protections: rate limiting and body size enforcement."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .config import settings

logger = logging.getLogger("agenttrafficcop.middleware")


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

    def __init__(self, max_per_window: int, window_seconds: int = 60) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._calls: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        """Record a call for *key*.

        Returns:
            (allowed, retry_after_seconds)
            - allowed=True  → request is within the limit
            - allowed=False → request is over the limit; retry_after is
                              how many seconds until the next slot opens
        """
        now = time.monotonic()
        cutoff = now - self._window
        calls = self._calls[key]

        # Drop timestamps outside the window.
        while calls and calls[0] < cutoff:
            calls.popleft()

        if len(calls) >= self._max:
            # Rate limited. Compute when the oldest in-window call will expire.
            oldest = calls[0]
            retry_after = max(1, int(self._window - (now - oldest)) + 1)
            return False, retry_after

        calls.append(now)
        return True, 0


_limiter = _SlidingWindowLimiter(
    max_per_window=settings.rate_limit_per_minute,
    window_seconds=60,
)


def _client_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes."""
    # Prefer the immediate client IP. In production behind a trusted
    # proxy, enable uvicorn's --proxy-headers so request.client reflects
    # the real client.
    if request.client is not None:
        return request.client.host
    return "unknown"


async def check_rate_limit(request: Request) -> None:
    """Enforce the rate limit for this request.

    Raises a `RateLimitError` (a `TrafficCopError` subclass) when the
    limit is exceeded. The exception handler converts it to a 429.
    """
    key = _client_key(request)
    allowed, retry_after = _limiter.check(key)
    if not allowed:
        from .exceptions import RateLimitError

        logger.warning("rate_limit_exceeded client=%s path=%s", key, request.url.path)
        raise RateLimitError(
            message=(
                f"Rate limit of {settings.rate_limit_per_minute} "
                f"requests/minute exceeded."
            ),
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
    """Wire HTTP-level protections into the app."""
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_body_bytes,
    )
    logger.info(
        "middleware_installed max_body_bytes=%d rate_limit_per_minute=%d",
        settings.max_body_bytes,
        settings.rate_limit_per_minute,
    )