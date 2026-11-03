"""Read-only safety preflight for the authorised India Pulse newsroom pilot."""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Match the other repository scripts when executed as `python scripts/...`.
sys.path.insert(0, ".")

from scripts.run_newsroom_pilot import PILOT_DATABASE_NAME, PilotStopError, _pilot_settings


EXPECTED_ALEMBIC_REVISION = "7922d051fd24"


async def preflight() -> dict[str, object]:
    settings, max_requests = _pilot_settings()
    if settings.x_api_enabled or settings.x_reads_enabled or settings.x_research_enabled:
        raise PilotStopError("pilot requires all X API access settings to be false")
    if settings.enable_reply_scanner or settings.ai_reply_publishing_enabled:
        raise PilotStopError("pilot requires reply scanning and reply publishing to be disabled")

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_database() AS database, current_user AS user, "
                        "inet_server_addr()::text AS host, inet_server_port() AS port"
                    )
                )
            ).mappings().one()
            if identity["database"] != PILOT_DATABASE_NAME:
                raise PilotStopError("connected database is not x_agent_pilot")
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            superuser = await connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
            if not vector_version:
                raise PilotStopError("pgvector extension is not installed")
            if revision != EXPECTED_ALEMBIC_REVISION:
                raise PilotStopError(
                    f"migration revision is {revision!r}; expected {EXPECTED_ALEMBIC_REVISION!r}"
                )
            if superuser:
                raise PilotStopError("pilot runtime role must not be a PostgreSQL superuser")
    finally:
        await engine.dispose()

    return {
        "database": identity["database"],
        "database_host": identity["host"],
        "database_port": identity["port"],
        "database_user": identity["user"],
        "pgvector": vector_version,
        "alembic_revision": revision,
        "scheduler_enabled": settings.scheduler_enabled,
        "x_publishing_mode": settings.x_publishing_mode,
        "publishing_enabled": settings.publishing_enabled,
        "dry_run": settings.dry_run,
        "x_api_enabled": settings.x_api_enabled,
        "x_reads_enabled": settings.x_reads_enabled,
        "x_research_enabled": settings.x_research_enabled,
        "model": settings.gemini_model,
        "request_limit": max_requests,
        "external_publishing_capability": False,
    }


def main() -> None:
    print(json.dumps(asyncio.run(preflight()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except PilotStopError as exc:
        print(f"PREFLIGHT BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
