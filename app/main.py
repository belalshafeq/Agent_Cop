"""FastAPI application factory for AgentTrafficCop."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from . import __version__
from .config import settings
from .exceptions import register_exception_handlers
from .logging_config import configure_logging
from .routes import router

# Install JSON logging before anything else emits a log line.
configure_logging()
logger = logging.getLogger("agenttrafficcop.main")


def _custom_openapi(app: FastAPI):
    """Inject a Bearer security scheme into the generated OpenAPI doc."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {})
    schema["components"].setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Send `Authorization: Bearer <your-api-key>`. "
            "Keys are configured server-side via ATC_API_KEYS."
        ),
    }
    app.openapi_schema = schema
    return schema


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    docs_enabled = not settings.is_production

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- startup ---
        logger.info(
            "service_started version=%s env=%s auth_enabled=%s "
            "max_steps=%d max_repeated=%d max_cycle_len=%d",
            __version__,
            settings.environment,
            settings.auth_enabled,
            settings.max_steps,
            settings.max_repeated_hashes,
            settings.max_cycle_length,
        )
        yield
        # --- shutdown ---
        logger.info(
            "service_stopped active_sessions=%d",
            settings.max_sessions,
        )

    app = FastAPI(
        title="AgentTrafficCop",
        description=(
            "Zero-config Multi-Agent Orchestration & Governance Middleware. "
            "Runtime circuit breaker and schema shield for autonomous "
            "agent swarms."
        ),
        version=__version__,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    app.include_router(router)

    # Only install the OpenAPI customizer when docs are enabled —
    # otherwise it just wastes memory building an unused schema.
    if docs_enabled:
        app.openapi = lambda: _custom_openapi(app)

    return app


# Module-level app instance. Uvicorn imports this as `app.main:app`.
app = create_app()