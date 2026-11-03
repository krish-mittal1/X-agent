"""Descriptive engagement evaluation.

No automated "learning" claim is made from tiny samples. This module computes
observable engagement rates and surfaces them for human review; any ranking
feedback would need enough data before being wired into scoring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.models import Post, PostMetric


async def evaluate_recent(
    session: AsyncSession, account_id: int, *, days: int = 30, limit: int = 50
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    latest_subquery = (
        select(
            PostMetric.post_id,
            func.max(PostMetric.fetched_at).label("latest"),
        )
        .group_by(PostMetric.post_id)
        .subquery()
    )
    stmt = (
        select(Post, PostMetric)
        .join(PostMetric, PostMetric.post_id == Post.id)
        .join(
            latest_subquery,
            (latest_subquery.c.post_id == PostMetric.post_id)
            & (latest_subquery.c.latest == PostMetric.fetched_at),
        )
        .where(Post.account_id == account_id, Post.published_at >= since)
        .order_by(Post.published_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    samples: list[dict[str, Any]] = []
    for post, metric in rows:
        impressions = max(metric.impressions, 1)
        engagements = metric.likes + metric.replies + metric.reposts + metric.quotes
        samples.append(
            {
                "x_post_id": post.x_post_id,
                "kind": post.kind,
                "published_at": post.published_at.isoformat(),
                "impressions": metric.impressions,
                "engagements": engagements,
                "engagement_rate": round(engagements / impressions, 4),
                "text": post.text[:160],
            }
        )
    if not samples:
        return {
            "sample_size": 0,
            "note": "no metrics collected yet (X read access may be unavailable)",
            "samples": [],
        }
    rates = [sample["engagement_rate"] for sample in samples]
    return {
        "sample_size": len(samples),
        "average_engagement_rate": round(sum(rates) / len(rates), 4),
        "top": sorted(samples, key=lambda item: item["engagement_rate"], reverse=True)[:5],
        "sample_note": "descriptive only; no automated ranking change is applied",
    }
