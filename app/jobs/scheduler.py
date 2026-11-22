"""APScheduler wiring for conservative, jittered, idempotent jobs."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.jobs.runner import JobContext, run_slot
from app.jobs.tasks import TASKS
from app.logging_config import get_logger
from app.memory.database import get_session_factory
from app.memory.embeddings import build_embedding_provider
from app.memory.repositories import JobRepository
from app.providers.factory import LLMService, build_provider
from app.publishing.x_client import XClient

logger = get_logger(__name__)

BROWSER_RESOURCE_JOBS = frozenset(
    {"publishing", "outbound_engagement", "viral_video_repost"}
)


def _job_plan(settings: Settings) -> list[tuple[str, int]]:
    return [
        ("rss_ingestion", settings.ingest_interval_minutes * 60),
        ("github_discovery", settings.ingest_interval_minutes * 60),
        ("ranking", settings.rank_interval_minutes * 60),
        ("drafting", settings.draft_interval_minutes * 60),
        ("publishing", settings.publish_interval_minutes * 60),
        ("growth_plan", settings.growth_plan_interval_minutes * 60),
        ("outbound_engagement", settings.outbound_engagement_interval_minutes * 60),
        ("reply_drafts", settings.reply_draft_interval_minutes * 60),
        ("memory_consolidation", settings.consolidation_interval_hours * 3600),
        ("analytics", settings.analytics_interval_hours * 3600),
        ("viral_video_repost", getattr(settings, "viral_video_repost_interval_minutes", 360) * 60),
    ]


def _timezone(settings: Settings) -> Any:
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("invalid timezone; falling back to UTC",
                       extra={"extra_fields": {"timezone": settings.timezone}})
        return ZoneInfo("UTC")


async def start_scheduler(settings: Settings) -> AsyncIOScheduler:
    await _reconcile_stale_job_runs()
    try:
        embedder = build_embedding_provider(settings)
    except Exception as exc:
        logger.error(
            "embedding provider unavailable; RAG indexing and semantic novelty disabled",
            extra={"extra_fields": {"error": str(exc)[:300]}},
        )
        embedder = None

    provider = build_provider(settings)
    llm = LLMService(settings, provider, get_session_factory())
    x_client = XClient(settings) if settings.x_write_credentials_present else None

    context = JobContext(
        settings=settings,
        session_factory=get_session_factory(),
        llm=llm,
        embedder=embedder,
        x_client=x_client,
    )

    scheduler = AsyncIOScheduler(timezone=_timezone(settings))
    for job_name, interval_seconds in _job_plan(settings):
        task = TASKS[job_name]

        async def _run(
            job_name: str = job_name,
            task: Any = task,
            interval_seconds: int = interval_seconds,
        ) -> None:
            resource_key = "brave_profile" if job_name in BROWSER_RESOURCE_JOBS else None
            await run_slot(
                job_name,
                context,
                task,
                interval_seconds=interval_seconds,
                resource_key=resource_key,
            )

        scheduler.add_job(
            _run,
            trigger=IntervalTrigger(seconds=max(60, interval_seconds), timezone=_timezone(settings)),
            id=job_name,
            name=job_name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            jitter=min(120, max(5, interval_seconds // 6)),
        )

    scheduler.start()
    scheduler.job_context = context  # type: ignore[attr-defined]
    logger.info(
        "scheduler started",
        extra={"extra_fields": {"jobs": [job.id for job in scheduler.get_jobs()]}},
    )
    # IntervalTrigger waits for the first interval before its first execution.
    # After a restart that made a healthy agent appear idle for hours (drafting
    # can be configured at 4h).  Seed the research/draft pipeline once at
    # startup; the regular interval jobs remain the source of truth afterwards.
    scheduler.startup_warmup_task = asyncio.create_task(  # type: ignore[attr-defined]
        _startup_warmup(context, settings)
    )
    if settings.llm_validate_model_on_startup and llm.enabled:
        asyncio.create_task(_validate_model(llm))
    return scheduler


async def _reconcile_stale_job_runs() -> None:
    """Mark jobs abandoned by a previous process as interrupted.

    PostgreSQL advisory locks disappear when the old process disconnects, but
    its audit row remains ``running``. Without reconciliation the health monitor
    stays unhealthy forever even though current jobs are executing normally.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    async with get_session_factory()() as session:
        count = await JobRepository.interrupt_stale(session, started_before=cutoff)
        if count:
            await session.commit()
            logger.warning(
                "reconciled stale scheduled job runs",
                extra={"extra_fields": {"count": count}},
            )


async def _startup_warmup(context: JobContext, settings: Settings) -> None:
    """Run a non-publishing discovery pass after startup.

    The warm-up deliberately stops before publishing.  It only ensures that
    fresh, verified drafts exist for the normal paced publishing job; it cannot
    bypass growth slots, policy gates, or the publishing kill switches.
    """
    try:
        for name, interval in (
            ("rss_ingestion", settings.ingest_interval_minutes * 60),
            ("github_discovery", settings.ingest_interval_minutes * 60),
            ("ranking", settings.rank_interval_minutes * 60),
            ("drafting", settings.draft_interval_minutes * 60),
        ):
            await run_slot(name, context, TASKS[name], interval_seconds=interval)
    except Exception as exc:  # startup must remain healthy if research is unavailable
        logger.exception(
            "startup warm-up failed",
            extra={"extra_fields": {"error": str(exc)[:300]}},
        )


async def _validate_model(llm: LLMService) -> None:
    try:
        ok, detail = await llm.provider.validate_model()  # type: ignore[attr-defined]
    except Exception as exc:  # never crash startup over catalogue lookup
        logger.warning("model validation failed", extra={"extra_fields": {"error": str(exc)[:200]}})
        return
    if ok:
        logger.info("LLM model available", extra={"extra_fields": {"detail": detail}})
    else:
        logger.error("LLM model unavailable", extra={"extra_fields": {"detail": detail}})


async def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    warmup = getattr(scheduler, "startup_warmup_task", None)
    if warmup is not None and not warmup.done():
        warmup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup
    scheduler.shutdown(wait=False)
    context: JobContext | None = getattr(scheduler, "job_context", None)
    if context is None:
        return
    await context.llm.aclose()
    if context.x_client is not None:
        await context.x_client.aclose()
