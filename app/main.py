"""FastAPI application factory.

The HTTP surface is deliberately small: health/readiness, the draft review queue
and metrics. There is no endpoint that publishes arbitrary text.
"""

from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.logging_config import configure_logging, get_logger, new_correlation_id
from app.memory.database import dispose_engine, init_engine

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, *, start_scheduler: bool | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, resolved.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        new_correlation_id()
        init_engine(resolved)
        app.state.settings = resolved
        app.state.scheduler = None
        logger.info(
            "application starting",
            extra={"extra_fields": {"env": resolved.app_env, "publishing_enabled": resolved.publishing_enabled,
                                    "dry_run": resolved.dry_run, "llm_enabled": resolved.llm_enabled,
                                    "zero_cost_mode": resolved.is_zero_cost_mode,
                                    "publishing_mode": resolved.x_publishing_mode}},
        )
        if resolved.is_zero_cost_mode:
            logger.info(
                "[OPERATOR_DISPATCH_MODE] Running in local draft queue mode. "
                "Outbound external API dispatch is disabled; content staged locally.",
                extra={"extra_fields": {"publishing_mode": resolved.x_publishing_mode}},
            )
        should_start = resolved.scheduler_enabled if start_scheduler is None else start_scheduler
        if should_start:
            from app.jobs.scheduler import start_scheduler as _start

            app.state.scheduler = await _start(resolved)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                from app.jobs.scheduler import shutdown_scheduler

                await shutdown_scheduler(app.state.scheduler)
            await dispose_engine()
            logger.info("application stopped")

    app = FastAPI(
        title="tech-x-agent",
        version="0.1.0",
        description="Research-driven X account agent with persistent RAG memory.",
        lifespan=lifespan,
    )
    app.state.settings = resolved

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        import app.logging_config as logging_config

        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16]
        logging_config.correlation_id_var.set(correlation_id)
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = request.headers.get("x-correlation-id", "-")
        logger.exception("unhandled error", extra={"extra_fields": {"path": request.url.path}})
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "correlation_id": correlation_id},
        )

    from app.api.dashboard import router as dashboard_router
    from app.api.drafts import router as drafts_router
    from app.api.editorial_routes import analytics_router
    from app.api.editorial_routes import router as editorial_router
    from app.api.health import router as health_router
    from app.api.metrics import router as metrics_router
    from app.api.research_routes import router as research_router

    app.include_router(health_router)
    app.include_router(dashboard_router)
    app.include_router(drafts_router)
    app.include_router(metrics_router)
    app.include_router(research_router)
    app.include_router(editorial_router)
    app.include_router(analytics_router)
    return app


app = create_app()
