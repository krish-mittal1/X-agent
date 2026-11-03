"""Concrete scheduled tasks. Every task is safe to re-run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.agent.reply_drafter import ReplyDrafter
from app.agent.writer import ContentWriter
from app.analytics.metrics import AnalyticsService
from app.ingestion.x_research import XMentionsIngestor
from app.jobs.runner import JobContext
from app.logging_config import get_logger
from app.memory.consolidation import MemoryConsolidator
from app.memory.models import Account, Draft, Interaction
from app.memory.repositories import AccountRepository, SourceRepository
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
    publisher = Publisher(
        context.settings, context.session_factory, context.x_client,
        embedder=context.embedder,
    )
    results = []
    for draft in ready:
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


TASKS = {
    "rss_ingestion": rss_ingestion,
    "github_discovery": github_discovery,
    "ranking": ranking,
    "drafting": drafting,
    "publishing": publishing,
    "reply_drafts": reply_drafts,
    "memory_consolidation": consolidation,
    "analytics": analytics,
}

__all__ = ["TASKS"]
