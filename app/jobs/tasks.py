"""Concrete scheduled tasks. Every task is safe to re-run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select

from app.agent.reply_drafter import ReplyDrafter
from app.agent.writer import ContentWriter
from app.analytics.metrics import AnalyticsService
from app.growth.commentary import assess_originality
from app.growth.pacing import (
    daily_slots,
    daily_target,
    next_slot,
    resolve_timezone,
    slot_due,
)
from app.growth.ramp import compute_allowance
from app.growth.service import GrowthService
from app.ingestion.x_research import XMentionsIngestor
from app.jobs.runner import JobContext
from app.logging_config import get_logger
from app.memory.consolidation import MemoryConsolidator
from app.memory.models import Account, Draft, Interaction
from app.memory.repositories import AccountRepository, PostRepository, SourceRepository
from app.publishing.publisher import Publisher
from app.research.discovery import DiscoveryService
from app.research.llm_classifier import CandidateClassifier

logger = get_logger(__name__)


async def _default_account(context: JobContext) -> Account | None:
    async with context.session_factory() as session:
        return await AccountRepository.get_default(session, context.settings.x_persona_handle)


async def _discovery(context: JobContext, source_filter: set[str]) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    classifier = (
        CandidateClassifier(context.llm, context.settings) if context.llm.enabled else None
    )
    service = DiscoveryService(
        context.settings, context.session_factory, embedder=context.embedder,
        classifier=classifier,
    )
    stats = await service.run(account.id, source_filter=source_filter, score=False)
    return {
        "discovered": stats.ingested.stored,
        "fetched": stats.ingested.fetched,
        "duplicates": stats.ingested.duplicates,
        "errors": stats.errors[:5],
    }


async def rss_ingestion(context: JobContext) -> dict[str, Any]:
    return await _discovery(context, {"rss"})


async def github_discovery(context: JobContext) -> dict[str, Any]:
    return await _discovery(context, {"github"})


async def ranking(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    classifier = (
        CandidateClassifier(context.llm, context.settings) if context.llm.enabled else None
    )
    service = DiscoveryService(
        context.settings, context.session_factory, embedder=context.embedder,
        classifier=classifier,
    )
    stats = await service.run(account.id, ingest=False, score=True)
    return {"scored": stats.scored, "above_threshold": stats.above_threshold,
            "errors": stats.errors[:5]}


async def drafting(context: JobContext) -> dict[str, Any]:
    if not context.settings.drafting_enabled:
        return {"status": "skipped", "reason": "drafting disabled"}
    if not context.llm.enabled:
        return {"status": "skipped", "reason": "LLM unavailable"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    async with context.session_factory() as session:
        cutoff = datetime.now(UTC) - timedelta(hours=context.settings.freshness_window_hours)
        candidates = await SourceRepository.ranked_candidates(
            session,
            account.id,
            min_score=context.settings.min_candidate_score,
            limit=context.settings.max_candidates_per_run,
            cutoff=cutoff,
        )
    if not candidates:
        return {"drafts": 0, "reason": "no ranked candidates"}
    writer = ContentWriter(
        context.settings, context.llm, context.session_factory, embedder=context.embedder
    )
    outcomes = []
    for document in candidates[: context.settings.max_posts_per_run]:
        outcome = await writer.create_original_draft(account.id, [document.id])
        outcomes.append({"draft_id": outcome.draft_id, "status": outcome.status,
                         "blocked": outcome.blocked_reasons[:3]})
    return {"drafts": outcomes}


async def publishing(context: JobContext) -> dict[str, Any]:
    if not context.settings.publishing_enabled:
        return {"status": "skipped", "reason": "publishing disabled"}
    if context.settings.x_publishing_mode == "local_draft":
        return {"status": "skipped", "reason": "local_draft mode active"}
    if context.settings.emergency_pause:
        return {"status": "skipped", "reason": "emergency pause"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    cutoff = datetime.now(UTC) - timedelta(hours=context.settings.freshness_window_hours)
    async with context.session_factory() as session:
        ready = list(
            (
                await session.scalars(
                    select(Draft)
                    .where(
                        Draft.account_id == account.id,
                        Draft.kind == "original",
                        Draft.status == "ready",
                        Draft.created_at >= cutoff,
                    )
                    .order_by(Draft.created_at.desc())
                    .limit(context.settings.max_posts_per_run)
                )
            ).all()
        )
    if not ready:
        return {"published": 0, "reason": "no ready drafts"}

    # --- growth envelope -------------------------------------------------
    # Publishing only happens inside today's humanised slots, and never above
    # the volume this account's age allows. Without this the scheduler posts on
    # a fixed interval, which is the clearest automation signal an account can
    # emit.
    now = datetime.now(UTC)
    allowance = compute_allowance(
        settings=context.settings,
        account_created_at=getattr(account, "created_at", None),
        now=now,
    )
    zone = resolve_timezone(context.settings)
    day = now.astimezone(zone).date()
    target_posts = min(
        allowance.posts_per_day,
        daily_target(
            seed=account.id,
            day=day,
            minimum=context.settings.posts_per_day_min,
            maximum=context.settings.posts_per_day_max,
        ),
        context.settings.max_original_posts_per_day,
    )
    async with context.session_factory() as session:
        originals_today = await PostRepository.count_published_since(
            session, account.id, now - timedelta(days=1), kind="original"
        )
    if originals_today >= target_posts:
        return {
            "published": 0,
            "reason": "daily growth allowance reached",
            "stage": allowance.stage,
            "posts_today": originals_today,
            "target_posts": target_posts,
        }
    slots = daily_slots(
        day=day,
        target_posts=target_posts,
        window_start_hour=context.settings.posting_window_start_hour,
        window_end_hour=context.settings.posting_window_end_hour,
        min_gap_minutes=context.settings.growth_min_post_gap_minutes,
        seed=account.id,
        tz=zone,
    )
    if slots and slot_due(
        slots, now, tolerance_minutes=context.settings.growth_slot_tolerance_minutes
    ) is None:
        upcoming = next_slot(slots, now)
        return {
            "published": 0,
            "reason": "outside today's posting slots",
            "next_slot": upcoming.at.isoformat() if upcoming else None,
            "stage": allowance.stage,
        }

    publisher = Publisher(
        context.settings, context.session_factory, context.x_client,
        embedder=context.embedder,
    )
    results = []
    for draft in ready:
        policy_report = draft.policy_report or {}
        is_operator = bool(
            policy_report.get("source") == "operator_command"
            or policy_report.get("operator_approved")
            or policy_report.get("operator_override")
        )
        originality = assess_originality(
            text="\n\n".join([draft.body, *(draft.thread_parts or [])]),
            settings=context.settings,
            source_headline=draft.title or draft.topic or "",
            is_operator=is_operator,
        )
        if not originality.passed:
            # Hold the draft rather than post a copy-paste headline: duplicate
            # content is what suppresses reach in the first place.
            results.append({
                "draft_id": draft.id,
                "status": "held_for_originality",
                "reasons": list(originality.issues)[:3],
            })
            continue
        result = await publisher.publish_original(draft.id)
        results.append({"draft_id": draft.id, "status": result.status,
                        "reasons": result.reasons[:3]})
        if result.status in {"skipped", "unknown", "failed"}:
            break
    return {"results": results}


async def reply_drafts(context: JobContext) -> dict[str, Any]:
    if not context.settings.drafting_enabled:
        return {"status": "skipped", "reason": "drafting disabled"}
    if not context.llm.enabled:
        return {"status": "skipped", "reason": "LLM unavailable"}
    if context.settings.is_zero_cost_mode or not context.settings.enable_reply_scanner:
        return {
            "status": "skipped",
            "reason": "automated reply scanner disabled; operator manual reply workflow active",
        }
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    synced = 0
    if context.x_client is not None:
        ingestor = XMentionsIngestor(
            context.settings, context.session_factory, context.x_client,
            embedder=context.embedder,
        )
        if ingestor.enabled:
            synced = await ingestor.sync(account.id)
    async with context.session_factory() as session:
        candidates = list(
            (
                await session.scalars(
                    select(Interaction)
                    .where(
                        Interaction.account_id == account.id,
                        Interaction.direction == "inbound",
                        Interaction.occurred_at >= datetime.now(UTC) - timedelta(hours=48),
                    )
                    .order_by(Interaction.occurred_at.desc())
                    .limit(10)
                )
            ).all()
        )
        pending = [
            interaction
            for interaction in candidates
            if not (interaction.metadata_ or {}).get("reply_draft_id")
            and not (interaction.metadata_ or {}).get("no_reply_recommended")
        ][:3]
    drafter = ReplyDrafter(
        context.settings, context.llm, context.session_factory, embedder=context.embedder
    )
    drafted = []
    for interaction in pending:
        draft = await drafter.draft_reply(account.id, interaction.x_post_id)
        if draft is not None:
            drafted.append({"draft_id": draft.id, "status": draft.status})
    return {"synced_mentions": synced, "drafts": drafted}


async def consolidation(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    async with context.session_factory() as session:
        report = await MemoryConsolidator(context.settings).run(session, account.id)
        await session.commit()
    return report


async def analytics(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    service = AnalyticsService(
        context.settings, context.session_factory, context.x_client
    )
    return await service.collect(account.id)


async def growth_plan(context: JobContext) -> dict[str, Any]:
    """Build and persist today's growth plan.

    Posts are scheduled by the humanised slots above; replies and follows are
    queued here for the operator (nothing is sent automatically), and the
    coordination guard has already removed anything that would link accounts.
    """
    if not context.settings.growth_enabled:
        return {"status": "skipped", "reason": "growth layer disabled"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    service = GrowthService(context.settings, context.session_factory)
    plan = await service.build(account)
    queued = await service.persist(plan)
    return {
        "stage": plan.stage,
        "target_posts": plan.target_posts,
        "slots": [slot.at.isoformat() for slot in plan.slots],
        "reply_targets": [target.handle for target in plan.reply_targets],
        "follow_targets": plan.follow_targets,
        "queued_actions": queued,
        "rejected_actions": len(plan.rejected_actions),
        "link_budget": plan.link_budget,
        "notes": plan.notes[:5],
    }


async def outbound_engagement(context: JobContext) -> dict[str, Any]:
    """Autonomous Outbound Viral Engagement Worker.

    Scans top Indian viral accounts (@IndianTechGuide, @NewsAlgebraIND, @ANI),
    identifies unengaged breaking posts, generates sharp takes with Gemini,
    and publishes live via Brave CDP without manual intervention.
    """
    if (
        not getattr(context.settings, "outbound_engagement_enabled", True)
        or not context.settings.publishing_enabled
        or context.settings.dry_run
    ):
        return {
            "status": "skipped",
            "reason": "publishing or outbound engagement disabled / dry run",
        }
    if context.settings.emergency_pause:
        return {"status": "skipped", "reason": "emergency pause"}

    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}

    from app.growth.engagement import EngagementService
    from app.publishing.brave_cdp import BraveCDPPublisher, resolve_brave_executable

    publisher = BraveCDPPublisher(context.settings, context.session_factory)
    if not await publisher.is_cdp_available() and not resolve_brave_executable():
        return {"status": "skipped", "reason": "brave cdp unavailable"}

    service = EngagementService(
        context.settings,
        context.llm,
        context.session_factory,
        publisher=publisher,
    )

    target_handles = list(context.settings.growth_follow_handles) or [
        "gharkekalesh",
        "sagarcasm",
        "GabbbarSingh",
        "IndianTechGuide",
        "NewsAlgebraIND",
        "memenist",
        "peakbengaluru",
        "ANI",
    ]

    candidates = await service.scan_handles_for_candidates(target_handles, limit_per_handle=2)
    if not candidates:
        return {"status": "skipped", "reason": "no candidates found from scanned handles"}

    # Filter out tweets we already engaged with
    async with context.session_factory() as session:
        existing_drafts = await session.scalars(
            select(Draft.policy_report).where(
                Draft.account_id == account.id,
                Draft.kind.in_(["reply", "original"]),
            )
        )
        engaged_ids = set()
        for report in existing_drafts:
            if isinstance(report, dict):
                tid = report.get("target_post_id")
                if tid:
                    engaged_ids.add(str(tid))

    fresh_candidates = [
        c for c in candidates if c.get("tweet_id") and str(c["tweet_id"]) not in engaged_ids
    ]
    if not fresh_candidates:
        return {"status": "skipped", "reason": "all recent candidates already engaged"}

    chosen = fresh_candidates[0]
    target_url = chosen["url"]
    target_text = chosen["text"]
    target_author = chosen["author"]

    bangers = await service.generate_bangers(target_text, target_author=target_author)
    action: Literal["reply", "quote"] = "reply"
    style = "value_add"
    chosen_content = bangers.get("value_add") or bangers.get("sarcastic", "")

    if not chosen_content:
        return {"status": "skipped", "reason": "failed to generate banger content"}

    draft = await service.create_engagement_draft(
        account_id=account.id,
        target_post_url=target_url,
        content=chosen_content,
        action=action,
        style=style,
    )

    logger.info(
        "Outbound engagement draft queued for review",
        extra={"extra_fields": {"target_url": target_url, "action": action, "draft_id": draft.id}},
    )
    return {
        "status": "needs_review",
        "action": action,
        "target_url": target_url,
        "author": target_author,
        "content": chosen_content,
        "draft_id": draft.id,
    }


TASKS = {
    "growth_plan": growth_plan,
    "rss_ingestion": rss_ingestion,
    "github_discovery": github_discovery,
    "ranking": ranking,
    "drafting": drafting,
    "publishing": publishing,
    "outbound_engagement": outbound_engagement,
    "reply_drafts": reply_drafts,
    "memory_consolidation": consolidation,
    "analytics": analytics,
}

__all__ = ["TASKS"]
