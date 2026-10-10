"""Custom exceptions and FastAPI handlers for AgentTrafficCop.

Governance violations raise `TrafficCopError` subclasses. The handlers
registered here convert them into structured JSON responses with
stable error codes and diagnostic details — never raw stack traces.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("agenttrafficcop.exceptions")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TrafficCopError(Exception):
    """Base class for all AgentTrafficCop governance errors."""

    status_code: int = 422
    error_code: str = "ATC_ERROR"

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        diagnostics: Dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.session_id = session_id
        self.diagnostics = diagnostics or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.error_code,
            "message": self.message,
            "session_id": self.session_id,
            "diagnostics": self.diagnostics,
        }


class SchemaValidationError(TrafficCopError):
    """Raised when a message fails Pydantic validation."""

    status_code = 422
    error_code = "SCHEMA_VALIDATION_FAILED"


class CircuitBreakerTripped(TrafficCopError):
    """Base class for breaker trips (step limit, loop, cycle)."""

    status_code = 422
    error_code = "CIRCUIT_BREAKER_TRIPPED"


class StepLimitExceeded(CircuitBreakerTripped):
    """Session exceeded the maximum number of steps."""

    error_code = "STEP_LIMIT_EXCEEDED"


class RepeatedPayloadLoop(CircuitBreakerTripped):
    """Identical payload hash seen too many times in a row."""

    error_code = "REPEATED_PAYLOAD_LOOP"


class CyclicPayloadLoop(CircuitBreakerTripped):
    """A short repeating cycle (e.g. A -> B -> A -> B) was detected."""

    error_code = "CYCLIC_PAYLOAD_LOOP"


# ---------------------------------------------------------------------------
# FastAPI exception handlers
# ---------------------------------------------------------------------------


def register_exception_handlers(app: FastAPI) -> None:
    """Attach structured JSON error handlers to the FastAPI app."""

    @app.exception_handler(TrafficCopError)
    async def _handle_traffic_cop(
        _request: Request, exc: TrafficCopError
    ) -> JSONResponse:
        logger.warning(
            "governance_violation error_code=%s session_id=%s",
            exc.error_code,
            exc.session_id,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Convert FastAPI's validation errors into our structured format.

        FastAPI's RequestValidationError carries the raw body via
        `exc.body` in most cases. We use it to extract the session_id
        for diagnostic purposes, but never echo the full body back
        (it could contain sensitive payloads).
        """
        session_id: str | None = None
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            raw_sid = body.get("session_id")
            if isinstance(raw_sid, str):
                session_id = raw_sid[:128]  # truncate defensively

        errors = []
        for err in exc.errors():
            loc = err.get("loc", [])
            # Convert tuples to lists for JSON serialization.
            loc_list = [str(item) for item in loc]
            errors.append(
                {
                    "loc": loc_list,
                    "msg": str(err.get("msg", "")),
                    "type": str(err.get("type", "")),
                }
            )

        payload = {
            "error": SchemaValidationError.error_code,
            "message": "Incoming agent message failed schema validation.",
            "session_id": session_id,
            "diagnostics": {"validation_errors": errors},
        }
        logger.warning(
            "schema_validation_failed session_id=%s errors=%d",
            session_id,
            len(errors),
        )
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(Exception)
    async def _handle_unexpected(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        """Last-resort handler — never leak stack traces to clients."""
        logger.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content={
                "error": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred.",
                "session_id": None,
                "diagnostics": {"exception": exc.__class__.__name__},
            },
        )