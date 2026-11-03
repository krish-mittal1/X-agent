"""Engagement metrics retrieval and evaluation.

Metrics depend on X API read access, which is not guaranteed on free/basic tiers.
Failures are logged and skipped; they never affect publishing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import Post, PostMetric
from app.publishing.x_client import XError, XPermissionError

logger = get_logger(__name__)


class AnalyticsService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        x_client,  # type: ignore[no-untyped-def]
        *,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.x_client = x_client
        self.clock = clock or (lambda: datetime.now(UTC))

    async def collect(self, account_id: int, *, limit: int = 50) -> dict[str, object]:
        if not self.settings.analytics_enabled:
            return {"status": "skipped", "reason": "analytics disabled"}
        if self.x_client is None or not getattr(self.x_client, "configured", False):
            return {"status": "skipped", "reason": "X client not configured"}
        async with self.session_factory() as session:
            posts = list(
                (
                    await session.scalars(
                        select(Post)
                        .where(Post.account_id == account_id, Post.x_post_id.is_not(None))
                        .order_by(Post.published_at.desc())
                        .limit(limit)
                    )
                ).all()
            )
            if not posts:
                return {"status": "ok", "stored": 0, "reason": "no published posts"}
            ids = [str(post.x_post_id) for post in posts if post.x_post_id]
            try:
                metrics = await self.x_client.get_posts_metrics(ids)
            except XPermissionError as exc:
                logger.warning("metrics unavailable with current API access",
                               extra={"extra_fields": {"error": str(exc)[:200]}})
                return {"status": "skipped", "reason": "insufficient X API read access"}
            except XError as exc:
                logger.warning("metrics fetch failed",
                               extra={"extra_fields": {"error": str(exc)[:200]}})
                return {"status": "failed", "reason": str(exc)[:200]}
            by_x_id = {str(post.x_post_id): post for post in posts}
            stored = 0
            for x_id, values in metrics.items():
                post = by_x_id.get(x_id)
                if post is None:
                    continue
                session.add(
                    PostMetric(
                        post_id=post.id,
                        impressions=values.get("impressions", 0),
                        likes=values.get("likes", 0),
                        replies=values.get("replies", 0),
                        reposts=values.get("reposts", 0),
                        quotes=values.get("quotes", 0),
                        bookmarks=values.get("bookmarks", 0),
                        fetched_at=self.clock(),
                        raw=values,
                    )
                )
                stored += 1
            await session.commit()
        return {"status": "ok", "stored": stored}
