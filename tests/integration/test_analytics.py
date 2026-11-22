from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.analytics.evaluation import evaluate_recent
from app.analytics.metrics import AnalyticsService
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.models import ActionLog, PostMetric
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
    enabled = settings.model_copy(update={"analytics_enabled": True, "x_reads_enabled": True,
                                          "x_api_enabled": True, "x_publishing_mode": "api"})
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
    enabled = settings.model_copy(update={"analytics_enabled": True, "x_reads_enabled": True,
                                          "x_api_enabled": True, "x_publishing_mode": "api"})
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


async def test_browser_partial_snapshots_persist_without_fabricated_zeros(
    settings, engine, account,
) -> None:
    views = [3, 8, 19, 22]
    tweets = []
    for index, count in enumerate(views, 1):
        x_id = str(900 + index)
        await create_published_post(account.id, x_id)
        tweets.append({
            "tweet_id": x_id, "url": f"https://x.com/{account.handle}/status/{x_id}",
            "impressions": count,
        })
    scanner = AsyncMock()
    scanner.scan_profile_tweets.return_value = tweets
    enabled = settings.model_copy(update={
        "analytics_enabled": True, "x_publishing_mode": "brave_cdp",
    })
    service = AnalyticsService(
        enabled, db_module.get_session_factory(), None, browser_scanner=scanner,
    )
    result = await service.collect(account.id)
    assert result["partial"] == 4 and result["stored"] == 0
    # Read back through a separate session, after the collector committed.
    async with session_scope() as session:
        entries = (await session.scalars(select(ActionLog).where(
            ActionLog.account_id == account.id,
            ActionLog.action == "analytics.metrics_observed",
        ).order_by(ActionLog.id))).all()
        assert await session.scalar(select(func.count(PostMetric.id))) == 0
    assert [entry.details["observed_counts"] for entry in entries] == [
        {"impressions": value} for value in views
    ]
    assert all("bookmarks" in entry.details["missing_fields"] for entry in entries)
