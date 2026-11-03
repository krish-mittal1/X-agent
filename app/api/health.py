"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from app.api.deps import get_request_settings
from app.logging_config import get_logger
from app.memory.database import database_reachable, get_engine, vector_schema_status

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe: process is up. Performs no dependency I/O."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    settings = get_request_settings(request)
    checks: dict[str, dict[str, object]] = {}

    db_ok, db_detail = await database_reachable()
    checks["database"] = {"ok": db_ok, "detail": db_detail}

    vector_ok = False
    vector_detail = "database unavailable"
    if db_ok:
        vector_ok, vector_detail = await vector_schema_status(settings.embedding_dimensions)
    checks["pgvector"] = {"ok": vector_ok, "detail": vector_detail}

    migration_ok = False
    migration_detail = "database unavailable"
    if db_ok:
        try:
            async with get_engine().connect() as conn:
                version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            migration_ok = bool(version)
            migration_detail = f"revision {version}" if version else "no revision applied"
        except Exception as exc:
            migration_detail = f"migrations not applied: {type(exc).__name__}"
    checks["migrations"] = {"ok": migration_ok, "detail": migration_detail}

    checks["llm"] = {
        "ok": settings.llm_enabled,
        "detail": (
            f"provider={settings.llm_provider} model={settings.gemini_model or '<unset>'}"
            if settings.llm_enabled
            else "disabled/unconfigured (non-LLM components still run)"
        ),
    }
    outbound_allowed = (
        settings.publishing_enabled
        and not settings.dry_run
        and settings.x_publishing_mode != "local_draft"
    )
    checks["publishing"] = {
        "ok": not settings.emergency_pause,
        "detail": (
            "paused"
            if settings.emergency_pause
            else f"mode={settings.x_publishing_mode} enabled={settings.publishing_enabled} dry_run={settings.dry_run} outbound_allowed={outbound_allowed}"
        ),
        "mode": settings.x_publishing_mode,
        "outbound_allowed": outbound_allowed,
    }

    critical_ok = db_ok and vector_ok and migration_ok
    if not critical_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if critical_ok else "degraded",
        "checks": checks,
        "app_env": settings.app_env,
    }
