from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.analytics.evaluation import evaluate_recent
from app.analytics.metrics import AnalyticsService
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.models import PostMetric
from app.memory.repositories import PostRepository
from app.publishing.x_client import XPermissionError


class FakeMetricsClient:
    configured = True

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requested: list[str] = []

    async def get_posts_metrics(self, post_ids: list[str]) -> dict[str, dict[str, int]]:
        self.requested = post_ids
        if self.error:
            raise self.error
        return {
            post_ids[0]: {
                "impressions": 1000,
                "likes": 12,
                "replies": 3,
                "reposts": 2,
                "quotes": 1,
                "bookmarks": 8,
            }
        }


async def create_published_post(account_id: int, x_id: str = "321") -> int:
    async with session_scope() as session:
        post = await PostRepository.create(
            session,
            account_id=account_id,
            x_post_id=x_id,
            kind="original",
            position=0,
            text="Paged KV cache note",
            published_at=datetime.now(UTC),
        )
        await session.commit()
        return post.id


async def test_metrics_are_collected_and_evaluated(settings, engine, account) -> None:
    await create_published_post(account.id)
    enabled = settings.model_copy(update={"analytics_enabled": True})
    service = AnalyticsService(enabled, db_module.get_session_factory(), FakeMetricsClient())
    result = await service.collect(account.id)
    assert result["status"] == "ok"
    assert result["stored"] == 1

    async with session_scope() as session:
        metric = await session.scalar(select(PostMetric))
        evaluation = await evaluate_recent(session, account.id)
        count = await session.scalar(select(func.count(PostMetric.id)))
    assert metric is not None and metric.impressions == 1000
    assert count == 1
    assert evaluation["sample_size"] == 1
    assert evaluation["average_engagement_rate"] > 0
    assert evaluation["sample_note"]


async def test_metrics_disabled_is_skipped(settings, engine, account) -> None:
    await create_published_post(account.id, "322")
    service = AnalyticsService(settings, db_module.get_session_factory(), FakeMetricsClient())
    result = await service.collect(account.id)
    assert result["status"] == "skipped"


async def test_permission_error_does_not_break_job(settings, engine, account) -> None:
    await create_published_post(account.id, "323")
    enabled = settings.model_copy(update={"analytics_enabled": True})
    service = AnalyticsService(
        enabled,
        db_module.get_session_factory(),
        FakeMetricsClient(XPermissionError("no read access")),
    )
    result = await service.collect(account.id)
    assert result["status"] == "skipped"
    assert "read access" in str(result["reason"])
    async with session_scope() as session:
        metrics = (await session.scalars(select(PostMetric))).all()
    assert metrics == []
