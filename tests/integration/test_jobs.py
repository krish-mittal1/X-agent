from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.jobs.runner import JobContext, run_slot
from app.jobs.scheduler import shutdown_scheduler, start_scheduler
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.models import JobRun


def make_context(settings) -> JobContext:  # type: ignore[no-untyped-def]
    return JobContext(
        settings=settings,
        session_factory=db_module.get_session_factory(),
        llm=None,  # type: ignore[arg-type]
        embedder=None,
        x_client=None,
    )


async def test_job_runs_once_per_slot(settings, engine) -> None:
    calls = {"n": 0}

    async def task(context: JobContext) -> dict[str, object]:
        calls["n"] += 1
        return {"calls": calls["n"]}

    context = make_context(settings)
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    first = await run_slot("test_job", context, task, interval_seconds=3600, now=now)
    second = await run_slot("test_job", context, task, interval_seconds=3600, now=now)
    later = await run_slot(
        "test_job", context, task, interval_seconds=3600,
        now=datetime(2026, 10, 28, 13, 0, tzinfo=UTC),
    )
    assert first["status"] == "succeeded"
    assert second["status"] == "skipped"
    assert later["status"] == "succeeded"
    assert calls["n"] == 2
    async with session_scope() as session:
        runs = (await session.scalars(select(JobRun))).all()
    assert len(runs) == 2
    assert all(run.status == "succeeded" for run in runs)


async def test_job_failure_is_recorded_not_raised(settings, engine) -> None:
    async def failing(context: JobContext) -> dict[str, object]:
        raise RuntimeError("boom")

    result = await run_slot(
        "failing_job", make_context(settings), failing,
        interval_seconds=60, now=datetime(2026, 10, 28, 12, 0, tzinfo=UTC),
    )
    assert result["status"] == "failed"
    assert "boom" in result["error"]
    async with session_scope() as session:
        run = await session.scalar(select(JobRun).where(JobRun.job_name == "failing_job"))
    assert run is not None and run.status == "failed"


async def test_scheduler_registers_all_jobs(settings, engine) -> None:
    scheduler = await start_scheduler(settings)
    try:
        ids = {job.id for job in scheduler.get_jobs()}
    finally:
        await shutdown_scheduler(scheduler)
    assert ids == {
        "rss_ingestion",
        "github_discovery",
        "ranking",
        "drafting",
        "publishing",
        "reply_drafts",
        "memory_consolidation",
        "analytics",
    }
