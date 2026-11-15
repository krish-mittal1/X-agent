"""Growth service: reads account state, builds and persists the daily plan.

Persistence reuses the existing append-only ``action_logs`` table. A planned or
executed follow/like/reply is one audit row whose ``details`` carry the action,
which means the coordination guard's history survives restarts without a new
schema, and every growth action stays auditable like the rest of the system.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.growth.alts import GrowthAction
from app.growth.planner import DailyGrowthPlan, ReplyTarget, build_plan
from app.memory.models import Account, ActionLog, Interaction, Post
from app.memory.repositories import PostRepository
from app.publishing.text_rules import extract_links

PLAN_ACTION = "growth.action"
RECORDED_ACTION = "growth.action.executed"


class GrowthService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    async def recent_actions(self, account_id: int, *, days: int = 7) -> list[GrowthAction]:
        """Recent planned/executed actions for this account (guard history)."""
        since = self.clock() - timedelta(days=max(1, days))
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ActionLog)
                        .where(
                            ActionLog.account_id == account_id,
                            ActionLog.action.in_([PLAN_ACTION, RECORDED_ACTION]),
                            ActionLog.created_at >= since,
                        )
                        .order_by(ActionLog.created_at.desc())
                        .limit(500)
                    )
                ).all()
            )
        actions: list[GrowthAction] = []
        for row in rows:
            details = row.details or {}
            try:
                at = datetime.fromisoformat(str(details.get("at")))
            except (TypeError, ValueError):
                at = row.created_at
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            actions.append(
                GrowthAction(
                    kind=str(details.get("kind", "post")),  # type: ignore[arg-type]
                    at=at,
                    account_id=int(details.get("account_id", account_id) or account_id),
                    target_handle=str(details.get("target_handle", "") or ""),
                    target_x_user_id=str(details.get("target_x_user_id", "") or ""),
                    note=str(details.get("note", "") or ""),
                )
            )
        return actions

    async def reply_candidates(self, account_id: int, *, days: int = 7) -> list[ReplyTarget]:
        """People who have talked to us recently: the safest reply targets.

        Follower counts are not read here (that needs an X API read), so the
        mid-size filter passes them through with an explicit reason.
        """
        since = self.clock() - timedelta(days=max(1, days))
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Interaction.author_handle, Interaction.author_x_user_id)
                    .where(
                        Interaction.account_id == account_id,
                        Interaction.direction == "inbound",
                        Interaction.occurred_at >= since,
                        Interaction.author_handle.is_not(None),
                    )
                    .order_by(Interaction.occurred_at.desc())
                    .limit(200)
                )
            ).all()
        seen: dict[str, ReplyTarget] = {}
        for handle, x_user_id in rows:
            clean = (handle or "").strip().lstrip("@")
            if not clean or clean.lower() in seen:
                continue
            seen[clean.lower()] = ReplyTarget(
                handle=clean,
                x_user_id=str(x_user_id or ""),
                followers=None,
                score=1.0,
                reason="replied to us recently",
            )
        return list(seen.values())

    async def link_posts_today(self, account_id: int) -> int:
        since = self.clock() - timedelta(days=1)
        async with self.session_factory() as session:
            posts = list(
                (
                    await session.scalars(
                        select(Post).where(
                            Post.account_id == account_id,
                            Post.kind == "original",
                            Post.published_at >= since,
                        )
                    )
                ).all()
            )
        return sum(1 for post in posts if extract_links(post.text or ""))

    async def originals_today(self, account_id: int) -> int:
        async with self.session_factory() as session:
            return await PostRepository.count_published_since(
                session, account_id, self.clock() - timedelta(days=1), kind="original"
            )

    # ------------------------------------------------------------------
    # plan
    # ------------------------------------------------------------------
    async def build(self, account: Account) -> DailyGrowthPlan:
        follow_candidates = self.settings.growth_follow_handles
        return build_plan(
            settings=self.settings,
            account_id=account.id,
            account_created_at=getattr(account, "created_at", None),
            now=self.clock(),
            reply_candidates=await self.reply_candidates(account.id),
            follow_candidates=follow_candidates,
            recent_actions=await self.recent_actions(account.id),
            link_posts_today=await self.link_posts_today(account.id),
        )

    async def persist(self, plan: DailyGrowthPlan) -> int:
        """Store the plan's pending actions, once per account/day/kind/target."""
        day_key = plan.day.isoformat()
        created = 0
        async with self.session_factory() as session:
            existing = {
                (
                    str((row.details or {}).get("kind")),
                    str((row.details or {}).get("target_handle", "")).lower(),
                )
                for row in (
                    await session.scalars(
                        select(ActionLog).where(
                            ActionLog.account_id == plan.account_id,
                            ActionLog.action.in_([PLAN_ACTION, RECORDED_ACTION]),
                            ActionLog.details["day"].astext == day_key,
                        )
                    )
                ).all()
            }
            for action in plan.actions:
                key = (action.kind, action.target_handle.lower())
                if key in existing:
                    continue
                session.add(
                    ActionLog(
                        account_id=plan.account_id,
                        action=PLAN_ACTION,
                        outcome="planned",
                        details={**action.to_dict(), "day": day_key, "stage": plan.stage},
                    )
                )
                existing.add(key)
                created += 1
            await session.commit()
        return created

    async def record_action(
        self,
        *,
        account_id: int,
        kind: str,
        target_handle: str = "",
        target_x_user_id: str = "",
        note: str = "",
        outcome: str = "executed",
    ) -> dict[str, Any]:
        """Record an action the operator actually took (feeds the guard)."""
        at = self.clock()
        details = {
            "kind": kind,
            "at": at.isoformat(),
            "account_id": account_id,
            "target_handle": target_handle.lstrip("@"),
            "target_x_user_id": target_x_user_id,
            "note": note,
            "day": at.date().isoformat(),
        }
        async with self.session_factory() as session:
            row = ActionLog(
                account_id=account_id,
                action=RECORDED_ACTION,
                outcome=outcome,
                details=details,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return {"id": row.id, **details, "outcome": outcome}

    async def recent_records(
        self, account_id: int, *, limit: int = 50, kind: str | None = None
    ) -> Sequence[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ActionLog)
                        .where(
                            ActionLog.account_id == account_id,
                            ActionLog.action.in_([PLAN_ACTION, RECORDED_ACTION]),
                        )
                        .order_by(ActionLog.created_at.desc())
                        .limit(max(1, limit))
                    )
                ).all()
            )
        output = []
        for row in rows:
            details = dict(row.details or {})
            if kind and details.get("kind") != kind:
                continue
            output.append(
                {
                    "id": row.id,
                    "action": row.action,
                    "outcome": row.outcome,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    **details,
                }
            )
        return output
