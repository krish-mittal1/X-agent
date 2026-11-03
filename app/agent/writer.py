"""Research-grounded draft generation and deterministic validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.critic import EditorialCriticService
from app.agent.editorial_engine import EditorialEngine
from app.agent.formats import choose_format, requires_numeric_evidence
from app.agent.image_relevance import select_document_image
from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import (
    DRAFT_BLOCKED,
    DRAFT_NEEDS_REVIEW,
    DRAFT_READY,
    Draft,
    EditorialEvaluation,
    EvidenceClaim,
    SourceDocument,
)
from app.memory.repositories import DraftRepository, PostRepository, SourceRepository
from app.memory.retrieval import HybridRetriever
from app.providers.base import LLMError
from app.providers.factory import LLMService
from app.publishing.text_rules import (
    find_duplicate,
    normalize_typography,
    validate_text_rules,
)
from app.research.event_service import EventIntelligenceService
from app.research.service import ResearchService
from app.research.verification import ClaimVerifier, has_unlisted_quantitative_assertion
from app.schemas.content import (
    ContentGenerationError,
    DraftOutcome,
    DuplicateReport,
    TextRuleReport,
    VerificationReport,
)

logger = get_logger(__name__)


class ContentWriter:
    def __init__(
        self,
        settings: Settings,
        llm: LLMService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        embedder: EmbeddingProvider | None = None,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.session_factory = session_factory
        self.embedder = embedder
        self.verifier = ClaimVerifier(settings)
        self.critic = EditorialCriticService()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.event_service = EventIntelligenceService(settings, embedder=embedder)
        self.research_service = ResearchService(settings, llm=llm)
        self.editorial_engine = EditorialEngine(
            settings, llm, critic=self.critic, event_service=self.event_service
        )

    async def _load_sources(
        self, session: AsyncSession, account_id: int, source_ids: list[int]
    ) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        for source_id in source_ids:
            document = await SourceRepository.get(session, account_id, source_id)
            if document is None:
                raise ValueError(f"source document {source_id} not found for this account")
            if document.fetch_status == "failed":
                continue
            documents.append(document)
        if not documents:
            raise ValueError("no usable source documents supplied")
        return documents

    async def _recent_formats(
        self, session: AsyncSession, account_id: int, limit: int = 5
    ) -> list[str]:
        """Return prior validation formats for the configured format mix.

        Story classification and editorial angle are independent of this
        selection. The configured mix only controls deterministic policy
        latitude (for example, whether an explicitly selected joke format may
        contain hyperbolic numbers without source-backed reporting claims).
        """
        rows = await session.execute(
            select(Draft.policy_report)
            .where(Draft.account_id == account_id, Draft.kind == "original")
            .order_by(Draft.id.desc())
            .limit(limit)
        )
        return [
            report["validation_format"]
            for report in rows.scalars()
            if isinstance(report, dict) and isinstance(report.get("validation_format"), str)
        ]

    async def create_original_draft(
        self, account_id: int, source_ids: list[int]
    ) -> DraftOutcome:
        if not self.settings.drafting_enabled:
            raise ContentGenerationError("drafting is disabled by configuration")
        key = f"original:{account_id}:{'-'.join(str(i) for i in sorted(source_ids))}"

        async with self.session_factory() as session:
            existing = await DraftRepository.get_by_key(session, account_id, key)
            if existing is not None:
                return self._outcome_from_draft(existing)
            documents = await self._load_sources(session, account_id, source_ids)
            primary_doc = documents[0]
            validation_format = choose_format(
                self.settings, await self._recent_formats(session, account_id)
            ).format

        # -------------------------------------------------------------------
        # 1. Multi-Source Research Engine (Stage 1)
        # -------------------------------------------------------------------
        async with self.session_factory() as session:
            report = await self.research_service.conduct_research(
                session,
                account_id,
                topic=primary_doc.title,
                candidate_source_id=primary_doc.id,
                max_sources=6,
            )
            # Safely extract loaded sources without triggering async lazy loader
            research_sources = getattr(report, "_loaded_sources", None)
            if research_sources is None and report:
                research_sources = list(report.__dict__.get("sources", []))
            research_sources = research_sources or []
            research_conflicts = list(report.conflicting_info or []) if report and hasattr(report, "conflicting_info") else []

        # -------------------------------------------------------------------
        # 2. Event Intelligence & Timeline Memory Retrieval (Stage 2 & 3)
        # -------------------------------------------------------------------
        async with self.session_factory() as session:
            event, is_new_event, delta_analysis = await self.event_service.match_or_create_event(
                session, account_id, report
            )
            mem_ctx = await self.event_service.retrieve_event_memory_context(
                session, account_id, event.id
            )
            event_memory_block = self.event_service.format_memory_prompt_block(mem_ctx)

        # -------------------------------------------------------------------
        # 3. Editorial Angle Generation & Evaluation (Stage 4)
        # -------------------------------------------------------------------
        try:
            selection_result = await self.editorial_engine.generate_and_evaluate(
                topic=primary_doc.title,
                documents=documents,
                event_memory_block=event_memory_block,
                event_id=event.id,
                research_report_id=report.id,
            )
        except LLMError as exc:
            logger.warning("draft generation failed", extra={"extra_fields": {"error": str(exc)[:300]}})
            raise ContentGenerationError(f"draft generation failed: {exc}") from exc

        champion = selection_result.champion
        alternatives = selection_result.alternatives

        # -------------------------------------------------------------------
        # 4. Independent Claim Verification on Candidates (Stage 5)
        # -------------------------------------------------------------------
        # Combine candidate document with all retrieved multi-source research documents
        evidence_sources: list[Any] = list(documents)
        existing_urls = {getattr(d, "url", "") for d in documents}
        for s in research_sources:
            if s.url and s.url not in existing_urls:
                evidence_sources.append(s)
                existing_urls.add(s.url)

        verification: VerificationReport = self.verifier.verify(
            champion.claims,
            evidence_sources,
            now=self.clock(),
            conflicting_claims=research_conflicts,
        )
        alt_verifications: list[dict[str, Any]] = []
        for alt in alternatives:
            alt_verif = self.verifier.verify(
                alt.claims,
                evidence_sources,
                now=self.clock(),
                conflicting_claims=research_conflicts,
            )
            alt_verifications.append(alt_verif.model_dump(mode="json"))

        parts = [normalize_typography(part) for part in champion.parts if part.strip()]
        parts = [part for part in parts if part]
        text_rules: TextRuleReport = validate_text_rules(
            self.settings, parts, format=validation_format
        )
        joined = "\n\n".join(parts)

        # -------------------------------------------------------------------
        # 5. Duplicate and Novelty Checks
        # -------------------------------------------------------------------
        embedding_similarity: float | None = None
        async with self.session_factory() as session:
            retriever = HybridRetriever(self.settings, self.embedder)
            novelty = await retriever.novelty_score(
                session, account_id=account_id, text=joined
            )
            embedding_similarity = None if novelty is None else max(0.0, 1.0 - novelty)
            recent_posts = await PostRepository.recent(
                session, account_id, limit=self.settings.recent_posts_for_dedup
            )
        duplicate: DuplicateReport = find_duplicate(
            text=joined,
            recent_texts=[post.text for post in recent_posts],
            settings=self.settings,
            embedding_similarity=embedding_similarity,
        )

        # -------------------------------------------------------------------
        # 6. Deterministic Policy Gate & Hard-Stop Safety Rules
        # -------------------------------------------------------------------
        blocking: list[str] = []
        blocking.extend(text_rules.issues)
        if not verification.passed:
            blocking.extend(verification.issues)
        blocking.extend(duplicate.reasons)

        if verification.hard_stop_triggered:
            blocking.append("hard stop: factual contradiction or unverified numeric claims")
        if verification.contradicted_claims > 0:
            blocking.append(f"draft contains {verification.contradicted_claims} contradicted claims")

        if requires_numeric_evidence(validation_format) and has_unlisted_quantitative_assertion(
            joined, champion.claims
        ):
            blocking.append("post contains quantitative claims without a declared supporting quote")

        if champion.critic_score and champion.critic_score.verdict == "rejected":
            blocking.append(champion.critic_score.critic_reasoning)
        if selection_result.status == "no_post_worthy_angle":
            blocking.append(
                "NO_POST_WORTHY_ANGLE: no sufficiently interesting or well-supported post"
            )

        if blocking:
            status = DRAFT_BLOCKED
        elif selection_result.status == "needs_revision" or report.status == "failed" and (not verification.passed or verification.unverified_facts > 0):
            status = DRAFT_NEEDS_REVIEW
        else:
            status = DRAFT_READY

        image_decision = select_document_image(documents, selection_result.topic)
        image_url = image_decision.image_url if image_decision.attach else None
        if champion.critic_score and image_url:
            champion.critic_score = self.critic.evaluate(
                draft_text=joined,
                claims=champion.claims,
                verification_report=verification,
                topic=selection_result.topic,
                angle=champion.angle,
                news_format=champion.news_format,
                image_url=image_url,
                image_relevant=image_decision.relevant,
                image_verified=image_decision.verified,
            )

        # -------------------------------------------------------------------
        # 7. Database Persistence & Memory Linking (Stage 7)
        # -------------------------------------------------------------------
        async with self.session_factory() as session:
            policy_report_dict: dict[str, Any] = {
                "duplicate": duplicate.model_dump(mode="json"),
                "text_rules": text_rules.model_dump(mode="json"),
                "critic": champion.critic_score.model_dump(mode="json") if champion.critic_score else None,
                "trending": any(doc.metadata_.get("trending") for doc in documents),
                "format": champion.news_format,
                "validation_format": validation_format,
                "blocking": blocking,
                "research_report_id": report.id,
                "event_id": event.id,
                "event_title": event.title,
                "event_delta": delta_analysis,
                "alternative_angles": [alt.to_dict() for alt in alternatives],
                "alternatives_verification": alt_verifications,
                "champion_details": champion.to_dict(),
            }
            if image_url:
                policy_report_dict["image_url"] = image_url
                policy_report_dict["image_source_url"] = image_decision.source_url
                policy_report_dict["image_relevant"] = image_decision.relevant
                policy_report_dict["image_verified"] = image_decision.verified
                policy_report_dict["image_reason"] = image_decision.reason
                if image_decision.credit:
                    policy_report_dict["image_credit"] = image_decision.credit

            draft = await DraftRepository.create(
                session,
                account_id=account_id,
                kind="original",
                status=status,
                idempotency_key=key,
                topic=selection_result.topic[:500],
                title=selection_result.topic[:500],
                body=parts[0] if parts else "",
                thread_parts=parts[1:],
                tone=champion.angle,
                rationale=champion.rationale,
                source_document_ids=[doc.id for doc in documents],
                candidate_score=documents[0].candidate_score,
                verification_report=verification.model_dump(mode="json"),
                policy_report=policy_report_dict,
                error="; ".join(blocking)[:4000] if blocking else None,
            )

            # Insert Critic evaluation record
            if champion.critic_score:
                session.add(
                    EditorialEvaluation(
                        draft_id=draft.id,
                        overall_score=champion.critic_score.overall_score,
                        factual_score=champion.critic_score.factual_score,
                        originality_score=champion.critic_score.originality_score,
                        insight_score=champion.critic_score.insight_score,
                        naturalness_score=champion.critic_score.naturalness_score,
                        persona_score=champion.critic_score.persona_score,
                        clarity_score=champion.critic_score.clarity_score,
                        verdict=champion.critic_score.verdict,
                        critic_reasoning=champion.critic_score.critic_reasoning,
                        flags=champion.critic_score.flags,
                        suggestions=champion.critic_score.suggestions,
                    )
                )

            # Link draft to ongoing event in memory
            await self.event_service.link_post_to_event(
                session,
                event_id=event.id,
                post_id=None,
                draft_id=draft.id,
                context_angle=champion.angle,
            )

            # Record editorial memory for future angle contrast
            await self.event_service.record_editorial_memory(
                session,
                account_id=account_id,
                topic=selection_result.topic,
                angle=champion.angle,
                format_name=champion.news_format,
                hook_type="news_hook",
                writing_notes=champion.rationale,
            )

            # Record evidence claims
            for check in verification.claims:
                if check.claim_type != "fact":
                    continue
                session.add(
                    EvidenceClaim(
                        draft_id=draft.id,
                        source_document_id=check.source_document_id,
                        claim_text=check.claim,
                        claim_type=check.claim_type,
                        supporting_quote=next(
                            (c.supporting_quote for c in champion.claims if c.claim == check.claim),
                            None,
                        ),
                        source_url=check.source_url,
                        retrieved_at=next(
                            (
                                doc.retrieved_at
                                for doc in documents
                                if doc.id == check.source_document_id
                            ),
                            None,
                        ),
                        status=check.status if check.status != "not_applicable" else "unverified",
                        confidence=check.confidence,
                        notes=check.notes,
                    )
                )

            await SourceRepository.mark_processed(
                session, account_id, [doc.id for doc in documents]
            )
            await session.commit()
            draft_id = draft.id

        logger.info(
            "original draft persisted with full intelligence pipeline",
            extra={
                "extra_fields": {
                    "draft_id": draft_id,
                    "event_id": event.id,
                    "research_report_id": report.id,
                    "champion_angle": champion.angle,
                    "status": status,
                    "blocking": len(blocking),
                }
            },
        )
        return DraftOutcome(
            draft_id=draft_id,
            status=status,
            blocked_reasons=blocking,
            verification=verification,
            text_rules=text_rules,
            duplicate=duplicate,
        )

    @staticmethod
    def _outcome_from_draft(draft: Draft) -> DraftOutcome:
        verification = VerificationReport.model_validate(draft.verification_report or {"passed": False})
        return DraftOutcome(
            draft_id=draft.id,
            status=draft.status,
            blocked_reasons=[draft.error] if draft.error else [],
            verification=verification,
            text_rules=TextRuleReport(passed=draft.status == DRAFT_READY, issues=[]),
            duplicate=DuplicateReport(duplicate=False),
        )

    async def draft_context(self, account_id: int, source_ids: list[int]) -> dict[str, Any]:
        """Debug helper: show exactly which sources/memory would be used."""
        async with self.session_factory() as session:
            documents = await self._load_sources(session, account_id, source_ids)
            return {
                "sources": [
                    {"id": doc.id, "title": doc.title, "url": doc.url, "status": doc.fetch_status}
                    for doc in documents
                ]
            }
