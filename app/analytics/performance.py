"""Performance Learning Engine (Feature 6).

Analyzes engagement metrics, operator feedback, and critic scores to create
continuous editorial feedback loops for @ssh_bugs without fabricating metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.models import Draft, EditorialEvaluation, Post, PostMetric


class AnglePerformance(BaseModel):
    angle: str
    sample_count: int = 0
    avg_impressions: float = 0.0
    avg_likes: float = 0.0
    avg_reposts: float = 0.0
    avg_replies: float = 0.0
    avg_bookmarks: float = 0.0
    engagement_rate: float = 0.0


class PerformanceInsight(BaseModel):
    category: str
    headline: str
    detail: str
    actionable_tip: str
    confidence: float = 0.85


class PerformanceReport(BaseModel):
    total_posts_analyzed: int = 0
    total_impressions: int = 0
    total_engagements: int = 0
    avg_engagement_rate: float = 0.0
    angle_performance: dict[str, AnglePerformance] = Field(default_factory=dict)
    top_performing_topics: list[str] = Field(default_factory=list)
    critic_approval_rate: float = 0.0
    insights: list[PerformanceInsight] = Field(default_factory=list)
    insufficient_data: bool = False
    generated_at: str = ""


class PerformanceAnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def generate_report(self, account_id: int, days: int = 30) -> PerformanceReport:
        cutoff = datetime.now(UTC) - timedelta(days=days)

        # 1. Query published posts with their metrics
        posts_query = (
            select(Post, PostMetric, Draft)
            .outerjoin(PostMetric, Post.id == PostMetric.post_id)
            .outerjoin(Draft, Post.draft_id == Draft.id)
            .where(Post.account_id == account_id, Post.published_at >= cutoff)
            .order_by(Post.published_at.desc())
        )
        result = await self.session.execute(posts_query)
        rows = result.all()

        total_posts = len(rows)

        # 2. Critic evaluations approval rate
        evals_query = (
            select(
                func.count(EditorialEvaluation.id),
                func.sum(case((EditorialEvaluation.verdict == "approved", 1), else_=0)),
            )
            .join(Draft, EditorialEvaluation.draft_id == Draft.id)
            .where(Draft.account_id == account_id, EditorialEvaluation.created_at >= cutoff)
        )
        eval_res = await self.session.execute(evals_query)
        eval_row = eval_res.first()
        total_evals = eval_row[0] if eval_row and eval_row[0] else 0
        approved_evals = eval_row[1] if eval_row and eval_row[1] else 0
        approval_rate = (
            round((approved_evals / total_evals) * 100, 1) if total_evals > 0 else 0.0
        )

        # If zero published posts exist, return strictly insufficient_data without synthetic numbers
        if total_posts == 0:
            return PerformanceReport(
                total_posts_analyzed=0,
                total_impressions=0,
                total_engagements=0,
                avg_engagement_rate=0.0,
                angle_performance={},
                top_performing_topics=[],
                critic_approval_rate=approval_rate,
                insights=[],
                insufficient_data=True,
                generated_at=datetime.now(UTC).isoformat(),
            )

        total_impressions = 0
        total_likes = 0
        total_reposts = 0
        total_replies = 0
        total_bookmarks = 0
        posts_with_metrics = 0

        angle_data: dict[str, dict[str, Any]] = {
            "analytical": {"count": 0, "imp": 0, "likes": 0, "reposts": 0, "replies": 0, "bm": 0},
            "contrarian": {"count": 0, "imp": 0, "likes": 0, "reposts": 0, "replies": 0, "bm": 0},
            "witty": {"count": 0, "imp": 0, "likes": 0, "reposts": 0, "replies": 0, "bm": 0},
        }
        topic_counts: dict[str, int] = {}

        for _post, metric, draft in rows:
            # Strictly do not fabricate missing metrics. If metric is None, skip engagement counting.
            if metric is None:
                continue

            posts_with_metrics += 1
            imp = metric.impressions
            likes = metric.likes
            reposts = metric.reposts
            replies = metric.replies
            bm = metric.bookmarks

            total_impressions += imp
            total_likes += likes
            total_reposts += reposts
            total_replies += replies
            total_bookmarks += bm

            angle = draft.tone if draft and draft.tone in angle_data else "analytical"
            entry = angle_data[angle]
            entry["count"] += 1
            entry["imp"] += imp
            entry["likes"] += likes
            entry["reposts"] += reposts
            entry["replies"] += replies
            entry["bm"] += bm

            if draft and draft.topic:
                clean_topic = draft.topic.split(":")[0][:40].strip()
                topic_counts[clean_topic] = topic_counts.get(clean_topic, 0) + 1

        if posts_with_metrics == 0:
            return PerformanceReport(
                total_posts_analyzed=total_posts,
                total_impressions=0,
                total_engagements=0,
                avg_engagement_rate=0.0,
                angle_performance={},
                top_performing_topics=[],
                critic_approval_rate=approval_rate,
                insights=[],
                insufficient_data=True,
                generated_at=datetime.now(UTC).isoformat(),
            )

        total_engagements = total_likes + total_reposts + total_replies + total_bookmarks
        avg_rate = round((total_engagements / max(1, total_impressions)) * 100, 2)

        angle_perf: dict[str, AnglePerformance] = {}
        for ang, d in angle_data.items():
            cnt = d["count"]
            if cnt > 0:
                imp = d["imp"]
                eng = d["likes"] + d["reposts"] + d["replies"] + d["bm"]
                rate = round((eng / max(1, imp)) * 100, 2)
                angle_perf[ang] = AnglePerformance(
                    angle=ang,
                    sample_count=cnt,
                    avg_impressions=round(imp / cnt, 1),
                    avg_likes=round(d["likes"] / cnt, 1),
                    avg_reposts=round(d["reposts"] / cnt, 1),
                    avg_replies=round(d["replies"] / cnt, 1),
                    avg_bookmarks=round(d["bm"] / cnt, 1),
                    engagement_rate=rate,
                )

        # 3. Generate genuine observations only when supported by observed data
        insights: list[PerformanceInsight] = []
        if (
            angle_perf.get("contrarian")
            and angle_perf.get("analytical")
            and angle_perf["contrarian"].sample_count >= 2
            and angle_perf["analytical"].sample_count >= 2
        ):
            c_rate = angle_perf["contrarian"].engagement_rate
            a_rate = angle_perf["analytical"].engagement_rate
            if c_rate > a_rate:
                diff = round(c_rate - a_rate, 1)
                insights.append(
                    PerformanceInsight(
                        category="angle_strategy",
                        headline="Contrarian angles generate higher audience engagement",
                        detail=f"Contrarian posts yielded {c_rate}% engagement vs {a_rate}% for analytical reporting (+{diff}% spread across observed posts).",
                        actionable_tip="Prioritize uncovering overlooked flaws or unintended policy consequences.",
                        confidence=0.85,
                    )
                )

        if total_evals >= 3:
            insights.append(
                PerformanceInsight(
                    category="editorial_critic",
                    headline="Critic evaluations active",
                    detail=f"Editorial Critic evaluated drafts with {approval_rate}% approval rate across {total_evals} persisted evaluations.",
                    actionable_tip="Review feedback on rejected candidates to adjust voice and anti-pattern constraints.",
                    confidence=0.90,
                )
            )

        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
        top_topics = [t[0] for t in sorted_topics[:5]]

        return PerformanceReport(
            total_posts_analyzed=total_posts,
            total_impressions=total_impressions,
            total_engagements=total_engagements,
            avg_engagement_rate=avg_rate,
            angle_performance=angle_perf,
            top_performing_topics=top_topics,
            critic_approval_rate=approval_rate,
            insights=insights,
            insufficient_data=False,
            generated_at=datetime.now(UTC).isoformat(),
        )
