"""Feedback Analytics Service: Aggregates human editorial decisions and trends."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.models import (
    FEEDBACK_APPROVED,
    FEEDBACK_EDITED,
    FEEDBACK_REJECTED,
    REJECTION_REASONS,
    Draft,
    EditorialFeedback,
)


class FeedbackAnalyticsService:
    """Computes pure human editorial feedback analytics with cohort breakdowns."""

    def __init__(self, session: AsyncSession, min_sample_threshold: int = 10) -> None:
        self.session = session
        self.min_sample_threshold = min_sample_threshold

    async def get_summary(self, account_id: int) -> dict[str, Any]:
        """Generate comprehensive editorial feedback analytics for an account."""
        # 1. Fetch all feedback records for account
        stmt = (
            select(EditorialFeedback, Draft.topic)
            .join(Draft, EditorialFeedback.draft_id == Draft.id, isouter=True)
            .where(EditorialFeedback.account_id == account_id)
            .order_by(EditorialFeedback.created_at.desc())
        )
        result = await self.session.execute(stmt)
        rows = result.all()

        total_reviewed = len(rows)
        insufficient_data = total_reviewed < self.min_sample_threshold

        if total_reviewed == 0:
            return {
                "insufficient_data": True,
                "sample_size": 0,
                "min_sample_threshold": self.min_sample_threshold,
                "total_reviewed": 0,
                "approval_count": 0,
                "rejection_count": 0,
                "edit_count": 0,
                "approval_rate": 0.0,
                "rejection_reasons_distribution": {r: 0 for r in REJECTION_REASONS},
                "angle_selection_distribution": {},
                "avg_critic_score_approved": 0.0,
                "avg_critic_score_rejected": 0.0,
                "avg_edit_reduction_pct": 0.0,
                "common_editing_patterns": [],
                "angle_breakdown": {},
                "topic_breakdown": {},
            }

        approval_count = 0
        rejection_count = 0
        edit_count = 0

        rejection_reasons_dist: dict[str, int] = {r: 0 for r in REJECTION_REASONS}
        angle_dist: dict[str, int] = {}
        approved_scores: list[float] = []
        rejected_scores: list[float] = []

        angle_cohorts: dict[str, dict[str, Any]] = {}
        topic_cohorts: dict[str, dict[str, Any]] = {}

        edit_reductions: list[float] = []
        editing_patterns: list[str] = []

        for fb, topic in rows:
            # Action counts
            if fb.action == FEEDBACK_APPROVED:
                approval_count += 1
                if fb.original_critic_score is not None:
                    approved_scores.append(fb.original_critic_score)
            elif fb.action == FEEDBACK_REJECTED:
                rejection_count += 1
                if fb.rejection_reason in rejection_reasons_dist:
                    rejection_reasons_dist[fb.rejection_reason] += 1
                elif fb.rejection_reason:
                    rejection_reasons_dist[fb.rejection_reason] = (
                        rejection_reasons_dist.get(fb.rejection_reason, 0) + 1
                    )
                if fb.original_critic_score is not None:
                    rejected_scores.append(fb.original_critic_score)
            elif fb.action == FEEDBACK_EDITED:
                edit_count += 1
                if fb.edited_text and fb.original_text_snapshot:
                    orig_len = len(fb.original_text_snapshot)
                    edit_len = len(fb.edited_text)
                    if orig_len > 0:
                        reduction = (orig_len - edit_len) / orig_len * 100.0
                        edit_reductions.append(reduction)

                    # Identify specific pattern
                    if edit_len < orig_len * 0.8:
                        editing_patterns.append("shortened_wordiness")
                    if fb.original_text_snapshot.count("?") > fb.edited_text.count("?"):
                        editing_patterns.append("replaced_rhetorical_question")

            # Angle distribution
            angle = fb.candidate_angle or "unknown"
            angle_dist[angle] = angle_dist.get(angle, 0) + 1

            # Angle cohorts
            if angle not in angle_cohorts:
                angle_cohorts[angle] = {"total": 0, "approved": 0, "rejected": 0, "edited": 0}
            angle_cohorts[angle]["total"] += 1
            if fb.action in angle_cohorts[angle]:
                angle_cohorts[angle][fb.action] += 1

            # Topic cohorts
            clean_topic = topic or "general"
            if clean_topic not in topic_cohorts:
                topic_cohorts[clean_topic] = {"total": 0, "approved": 0, "rejected": 0, "edited": 0}
            topic_cohorts[clean_topic]["total"] += 1
            if fb.action in topic_cohorts[clean_topic]:
                topic_cohorts[clean_topic][fb.action] += 1

        approval_rate = round(approval_count / total_reviewed, 4) if total_reviewed else 0.0
        avg_score_approved = (
            round(sum(approved_scores) / len(approved_scores), 2) if approved_scores else 0.0
        )
        avg_score_rejected = (
            round(sum(rejected_scores) / len(rejected_scores), 2) if rejected_scores else 0.0
        )
        avg_edit_reduction = (
            round(sum(edit_reductions) / len(edit_reductions), 2) if edit_reductions else 0.0
        )

        # Unique common patterns
        top_patterns = list(dict.fromkeys(editing_patterns))

        return {
            "insufficient_data": insufficient_data,
            "sample_size": total_reviewed,
            "min_sample_threshold": self.min_sample_threshold,
            "total_reviewed": total_reviewed,
            "approval_count": approval_count,
            "rejection_count": rejection_count,
            "edit_count": edit_count,
            "approval_rate": approval_rate,
            "rejection_reasons_distribution": rejection_reasons_dist,
            "angle_selection_distribution": angle_dist,
            "avg_critic_score_approved": avg_score_approved,
            "avg_critic_score_rejected": avg_score_rejected,
            "avg_edit_reduction_pct": avg_edit_reduction,
            "common_editing_patterns": top_patterns,
            "angle_breakdown": angle_cohorts,
            "topic_breakdown": topic_cohorts,
        }
