"""FastAPI application factory for AgentTrafficCop."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from . import __version__
from .audit import audit
from .config import settings
from .exceptions import register_exception_handlers
from .logging_config import configure_logging
from .middleware import install_middleware
from .routes import router

configure_logging()
logger = logging.getLogger("agenttrafficcop.main")


def _custom_openapi(app: FastAPI):
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
    docs_enabled = not settings.is_production

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "service_started version=%s env=%s auth_enabled=%s "
            "audit_enabled=%s audit_path=%s "
            "max_steps=%d max_repeated=%d max_cycle_len=%d",
            __version__,
            settings.environment,
            settings.auth_enabled,
            audit.enabled,
            str(audit.path) if audit.path else None,
            settings.max_steps,
            settings.max_repeated_hashes,
            settings.max_cycle_length,
        )
        yield
        logger.info("service_stopping")
        try:
            audit.close()
        except Exception:
            pass
        logger.info("service_stopped")

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

    # ---------------------------------------------------------------------
    # CORS
    # ---------------------------------------------------------------------
    # The landing page (and any future frontend) runs on a different origin
    # than the backend. The browser sends a preflight OPTIONS request before
    # any cross-origin POST — we must explicitly allow it here, or the
    # browser blocks the real request.
    #
    # Origins are read from ATC_CORS_ORIGINS (comma-separated). Defaults
    # cover local dev, Cloudflare Pages, and the Cloudflare Tunnel URL.
    #
    # Note: `allow_credentials=True` cannot be combined with `allow_origins=["*"]`.
    # We list explicit origins instead.
    allowed_origins = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
        "https://agent-cop.pages.dev",
        "https://agenttrafficcop.com",
        "https://www.agenttrafficcop.com",
    ]
    # Add anything from the environment.
    extra = [
        o.strip()
        for o in (settings.cors_origins or "").split(",")
        if o.strip()
    ]
    allowed_origins.extend(extra)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
        expose_headers=["Retry-After"],
        max_age=600,
    )

    install_middleware(app)
    register_exception_handlers(app)
    app.include_router(router)

    if docs_enabled:
        app.openapi = lambda: _custom_openapi(app)

    return app


app = create_app()