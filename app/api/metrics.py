"""Cost, usage and publishing metrics (authenticated)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import AdminAuth, DbSession
from app.memory.models import (
    ActionLog,
    Draft,
    ModelUsageRecord,
    Post,
    PostMetric,
)
from app.providers.factory import usage_snapshot

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/summary")
async def summary(_: AdminAuth, db: DbSession) -> dict[str, object]:
    now = datetime.now(UTC)
    day_start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)

    async def _count(stmt) -> int:  # type: ignore[no-untyped-def]
        return int(await db.scalar(stmt) or 0)

    posts_total = await _count(select(func.count(Post.id)))
    posts_week = await _count(select(func.count(Post.id)).where(Post.published_at >= week_start))
    posts_day = await _count(select(func.count(Post.id)).where(Post.published_at >= day_start))
    drafts_pending = await _count(
        select(func.count(Draft.id)).where(Draft.kind == "reply", Draft.status == "needs_review")
    )
    drafts_blocked = await _count(
        select(func.count(Draft.id)).where(Draft.status == "blocked")
    )
    failures = await _count(
        select(func.count(ActionLog.id)).where(
            ActionLog.outcome == "failed", ActionLog.created_at >= week_start
        )
    )
    spend_day = float(
        await db.scalar(
            select(func.coalesce(func.sum(ModelUsageRecord.estimated_cost_usd), 0.0)).where(
                ModelUsageRecord.created_at >= day_start
            )
        )
        or 0.0
    )
    spend_month = float(
        await db.scalar(
            select(func.coalesce(func.sum(ModelUsageRecord.estimated_cost_usd), 0.0)).where(
                ModelUsageRecord.created_at >= now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            )
        )
        or 0.0
    )

    top_posts = (
        await db.execute(
            select(Post.x_post_id, Post.text, PostMetric.likes, PostMetric.reposts, PostMetric.replies,
                   PostMetric.impressions)
            .join(PostMetric, PostMetric.post_id == Post.id)
            .order_by(PostMetric.fetched_at.desc())
            .limit(5)
        )
    ).all()

    return {
        "generated_at": now.isoformat(),
        "posts": {"total": posts_total, "last_24h": posts_day, "last_7d": posts_week},
        "drafts": {"reply_queue": drafts_pending, "blocked": drafts_blocked},
        "failures_last_7d": failures,
        "llm_spend": {"last_24h_usd": round(spend_day, 6), "month_usd": round(spend_month, 6)},
        "llm_usage_7d": await usage_snapshot(db),
        "recent_metrics": [
            {
                "x_post_id": row[0],
                "text": (row[1] or "")[:120],
                "likes": row[2],
                "reposts": row[3],
                "replies": row[4],
                "impressions": row[5],
            }
            for row in top_posts
        ],
    }
