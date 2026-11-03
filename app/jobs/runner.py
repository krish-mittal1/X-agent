"""Idempotent scheduled-job execution with cross-process locking."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.database import get_engine
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import JobRun
from app.memory.repositories import JobRepository
from app.providers.factory import LLMService

logger = get_logger(__name__)


@dataclass
class JobContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    llm: LLMService
    embedder: EmbeddingProvider | None
    x_client: Any | None


@asynccontextmanager
async def advisory_lock(key: str):  # type: ignore[no-untyped-def]
    conn = await get_engine().connect()
    got = False
    try:
        got = bool(
            await conn.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key}
            )
        )
        yield got
    finally:
        if got:
            await conn.scalar(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key}
            )
        await conn.close()


async def run_slot(
    job_name: str,
    context: JobContext,
    task: Callable[[JobContext], Awaitable[dict[str, Any]]],
    *,
    interval_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a task at most once per interval slot, at most once across processes."""
    reference = now or datetime.now(UTC)
    slot = int(reference.timestamp() // max(1, interval_seconds))
    run_key = f"{job_name}:{slot}"

    async with advisory_lock(job_name) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "job already running in another worker"}
        async with context.session_factory() as session:
            run = await JobRepository.begin(session, job_name, run_key)
            if run is None:
                await session.rollback()
                return {"status": "skipped", "reason": "slot already processed"}
            run_id = run.id
            await session.commit()

        status = "succeeded"
        details: dict[str, Any] = {}
        try:
            details = await task(context)
        except Exception as exc:
            status = "failed"
            details = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            logger.exception("scheduled job failed", extra={"extra_fields": {"job": job_name}})
        async with context.session_factory() as session:
            stored = await session.get(JobRun, run_id)
            if stored is not None:
                await JobRepository.finish(session, stored, status=status, details=details)
                await session.commit()
        logger.info(
            "job finished",
            extra={"extra_fields": {"job": job_name, "status": status, "details": details}},
        )
        return {"status": status, **details}
