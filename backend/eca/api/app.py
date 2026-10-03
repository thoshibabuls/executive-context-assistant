"""FastAPI application factory. Run with ``uvicorn eca.api.app:app``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from eca.api.assistant import router as assistant_router
from eca.api.auth import router as auth_router
from eca.api.chat import guidance_router
from eca.api.chat import router as chat_router
from eca.api.connections import router as connections_router
from eca.api.context import router as context_router
from eca.api.health import router as health_router
from eca.api.middleware import RequestIdMiddleware
from eca.api.problems import install_problem_handlers
from eca.api.projects import router as projects_router
from eca.api.ratelimit import RateLimiter
from eca.api.reminders import router as reminders_router
from eca.api.work import router as work_router
from eca.identity import JwksCache
from eca.intelligence import AIClient, AIError, build_ai_client
from eca.platform.config import Settings, get_settings
from eca.platform.cursors import CursorCodec
from eca.platform.db import create_engine, create_session_factory
from eca.platform.health import migration_head
from eca.platform.logging import configure_logging
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.platform.uow import UnitOfWorkFactory

ensure_selector_event_loop_policy()
log = structlog.get_logger("eca.api")


def _ai_client(settings: Settings, factory: UnitOfWorkFactory | None) -> AIClient | None:
    """The chat path's AI client (AI-04/05/06/07), metered as the API role. Without a database,
    a key or a valid configuration the API still starts and chat answers degrade (AI_PIPELINE.md §14)."""
    if factory is None:
        return None
    try:
        return build_ai_client(settings, uow_factory=factory)
    except AIError as exc:
        log.warning("ai_client_unavailable", error_type=type(exc).__name__)
        return None


def _init_sentry(settings: Settings) -> None:
    if settings.sentry_dsn is None or not settings.sentry_dsn.get_secret_value():
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.api_env,
        send_default_pii=False,
        max_request_body_size="never",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.api_log_level)
    _init_sentry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        url = settings.api_database_url.get_secret_value() if settings.api_database_url else None
        engine = create_engine(url) if url else None
        app.state.engine = engine
        app.state.uow_factory = UnitOfWorkFactory(create_session_factory(engine)) if engine else None
        app.state.migration_head = migration_head()
        app.state.http = httpx.AsyncClient()
        app.state.jwks = JwksCache(app.state.http)
        key = settings.cursor_signing_key.get_secret_value() if settings.cursor_signing_key else None
        app.state.cursors = CursorCodec(key)
        app.state.rate_limiter = RateLimiter()
        app.state.ai_client = _ai_client(settings, app.state.uow_factory)
        try:
            yield
        finally:
            await app.state.http.aclose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Executive Context Assistant API",
        version="0.0.1",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    install_problem_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "If-Match", "X-Request-Id"],
        )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(connections_router)
    app.include_router(work_router)
    app.include_router(context_router)
    app.include_router(assistant_router)
    app.include_router(projects_router)
    app.include_router(chat_router)
    app.include_router(guidance_router)
    app.include_router(reminders_router)
    return app


def get_uow_factory(request: Request) -> UnitOfWorkFactory:
    """Dependency for routers (slice 1.1+). Raises if the database is not configured."""
    factory = request.app.state.uow_factory
    if not isinstance(factory, UnitOfWorkFactory):
        raise RuntimeError("Database is not configured")
    return factory


app = create_app()
