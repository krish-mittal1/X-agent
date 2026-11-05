"""Explicit original-post workflow.

The LLM never chooses or executes tools. This module hard-codes the sequence:
discover -> rank -> draft with evidence -> verify (inside writer) -> policy gate
-> publish. Each step is independently skippable via configuration and safe to
re-run (idempotency keys + unique constraints).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.writer import ContentWriter
from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider
from app.memory.repositories import AccountRepository, SourceRepository
from app.providers.factory import LLMService
from app.providers.rate_limit import RateLimitExceededError
from app.publishing.publisher import Publisher
from app.research.discovery import DiscoveryService
from app.research.llm_classifier import CandidateClassifier

logger = get_logger(__name__)


@dataclass
class CycleReport:
    discovered: int = 0
    scored: int = 0
    candidates: int = 0
    drafts_created: int = 0
    drafts_blocked: int = 0
    published: int = 0
    simulated: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


class AgentOrchestrator:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMService,
        *,
        embedder: EmbeddingProvider | None = None,
        x_client=None,  # type: ignore[no-untyped-def]
        discovery: DiscoveryService | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.llm = llm
        self.embedder = embedder
        self.x_client = x_client
        classifier = CandidateClassifier(llm, settings) if llm.enabled else None
        self.discovery = discovery or DiscoveryService(
            settings, session_factory, embedder=embedder, classifier=classifier
        )
        self.writer = ContentWriter(
            settings, llm, session_factory, embedder=embedder
        )
        can_publish = (x_client is not None) or (
            settings.x_publishing_mode == "brave_cdp"
            and bool(settings.x_auth_token and settings.x_ct0)
        )
        self.publisher = (
            Publisher(settings, session_factory, x_client, embedder=embedder)
            if can_publish
            else None
        )

    async def _candidates(self, account_id: int) -> list:
        async with self.session_factory() as session:
            cutoff = datetime.now(UTC) - timedelta(hours=self.settings.freshness_window_hours)
            return await SourceRepository.ranked_candidates(
                session,
                account_id,
                min_score=self.settings.min_candidate_score,
                limit=self.settings.max_candidates_per_run,
                cutoff=cutoff,
            )

    async def run_cycle(self, account_id: int) -> CycleReport:
        report = CycleReport()
        async with self.session_factory() as session:
            account = await AccountRepository.get(session, account_id)
            if account is None:
                report.errors.append(f"account {account_id} not found")
                report.skipped_reason = "account missing"
                return report
            if not account.is_active:
                report.skipped_reason = "account inactive"
                return report

        if self.settings.discovery_enabled:
            try:
                stats = await self.discovery.run(account_id)
                report.discovered = stats.ingested.stored
                report.scored = stats.scored
                report.errors.extend(stats.errors)
            except Exception as exc:
                logger.exception("discovery step failed")
                report.errors.append(f"discovery failed: {type(exc).__name__}: {exc}")

        candidates = await self._candidates(account_id)
        report.candidates = len(candidates)
        if not candidates:
            report.skipped_reason = report.skipped_reason or "no eligible candidates"
            return report

        if not self.llm.enabled:
            report.skipped_reason = "LLM unavailable; candidates retained for later"
            return report
        if not self.settings.drafting_enabled:
            report.skipped_reason = "drafting disabled"
            return report

        for document in candidates:
            if report.drafts_created >= self.settings.max_posts_per_run:
                break
            try:
                outcome = await self.writer.create_original_draft(
                    account_id, [document.id]
                )
            except RateLimitExceededError as exc:
                # Daily quota is gone: trying the remaining candidates would only
                # produce the same failure, so stop and leave them queued.
                logger.warning(
                    "llm quota exhausted; stopping draft loop",
                    extra={"extra_fields": {"error": str(exc)[:200]}},
                )
                report.skipped_reason = f"llm quota exhausted: {exc}"
                break
            except Exception as exc:
                logger.warning(
                    "drafting failed for candidate",
                    extra={"extra_fields": {"source_id": document.id, "error": str(exc)[:200]}},
                )
                report.errors.append(f"draft failed for source {document.id}: {exc}")
                continue
            report.drafts_created += 1
            if outcome.status != "ready":
                report.drafts_blocked += 1
                continue
            if self.publisher is None:
                report.skipped_reason = "no X client configured; draft left ready"
                continue
            result = await self.publisher.publish_original(outcome.draft_id)
            if result.status == "published":
                report.published += 1
            elif result.status in ("simulated", "local_saved"):
                report.simulated += 1
            elif result.status == "blocked":
                report.drafts_blocked += 1
            else:
                report.errors.append(f"publish {result.status}: {'; '.join(result.reasons)}")

        logger.info(
            "agent cycle finished",
            extra={
                "extra_fields": {
                    "account_id": account_id,
                    "discovered": report.discovered,
                    "scored": report.scored,
                    "drafts": report.drafts_created,
                    "published": report.published,
                    "simulated": report.simulated,
                }
            },
        )
        return report
