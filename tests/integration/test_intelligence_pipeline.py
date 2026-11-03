"""Comprehensive Integration Test Suite for X Agent V2 Phase 2: Intelligence Engine.

Validates the full autonomous intelligence loop:
News Candidate -> Multi-Source Research -> Event Intelligence -> Historical Memory ->
Editorial Engine (3 angles) -> Claim Verification -> Editorial Critic (bounded rewrites) ->
Policy Gate -> Staged Draft in Review Queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.editorial_engine import (
    EditorialEngine,
    MultiAngleGenerationResponse,
    SingleAngleCandidate,
)
from app.agent.writer import ContentWriter
from app.config import Settings
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import (
    DRAFT_BLOCKED,
    DRAFT_READY,
    Account,
    Draft,
    EditorialMemory,
    Event,
    EventPostLink,
    EvidenceClaim,
    ResearchReport,
    SourceDocument,
)
from app.memory.repositories import SourceRepository
from app.providers.base import LLMResponse, LLMUsage
from app.research.schemas import (
    EntityItem,
    StructuredResearchReport,
)
from app.schemas.content import DraftClaim
from app.schemas.research import IngestedItem


class FakeIntelligenceLLM:
    """Mock LLM returning structured intelligence objects for multi-angle testing."""

    def __init__(
        self,
        *,
        research_report: StructuredResearchReport | None = None,
        multi_angle: MultiAngleGenerationResponse | None = None,
        rewrite_candidate: SingleAngleCandidate | None = None,
    ) -> None:
        self.research_report = research_report
        self.multi_angle = multi_angle
        self.rewrite_candidate = rewrite_candidate
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, messages, schema, operation="", **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"operation": operation, "schema": schema.__name__, "messages": list(messages)})
        if schema == StructuredResearchReport and self.research_report is not None:
            return self.research_report, LLMResponse(
                text="{}", model="fake-gemini", usage=LLMUsage(), latency_ms=1.0, estimated_cost_usd=0.0
            )
        if schema == MultiAngleGenerationResponse and self.multi_angle is not None:
            return self.multi_angle, LLMResponse(
                text="{}", model="fake-gemini", usage=LLMUsage(), latency_ms=1.0, estimated_cost_usd=0.0
            )
        if schema == SingleAngleCandidate and self.rewrite_candidate is not None:
            return self.rewrite_candidate, LLMResponse(
                text="{}", model="fake-gemini", usage=LLMUsage(), latency_ms=1.0, estimated_cost_usd=0.0
            )
        raise ValueError(f"FakeIntelligenceLLM unconfigured for schema {schema}")


@pytest.fixture
async def seeded_candidate(db: AsyncSession, account: Account) -> SourceDocument:
    item = IngestedItem(
        source_type="rss",
        source_identifier="indianexpress.com",
        url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
        title="Tata Electronics receives environmental clearance for ₹91,000 crore Dholera wafer plant",
        content=(
            "Tata Electronics has received final environmental and regulatory clearances "
            "for its mega semiconductor fabrication plant in Dholera, Gujarat. The ₹91,000 crore facility "
            "is being built in partnership with Taiwan's Powerchip Semiconductor Manufacturing Corp (PSMC). "
            "Commercial production of 28nm chips is slated for late 2026."
        ),
        excerpt="Tata Electronics secures green clearance for ₹91,000 crore Dholera fab.",
        published_at=datetime.now(UTC),
        reliability_score=0.9,
    )
    doc, _ = await SourceRepository.store_item(db, account.id, item)
    assert doc is not None
    doc.candidate_score = 0.88
    await db.commit()
    return doc


def build_mock_multi_angle() -> MultiAngleGenerationResponse:
    quote = "The ₹91,000 crore facility is being built in partnership with Taiwan's Powerchip Semiconductor Manufacturing Corp (PSMC)."
    return MultiAngleGenerationResponse(
        topic="Tata Electronics Dholera Fab",
        analytical=SingleAngleCandidate(
            angle="analytical",
            hook="Tata's ₹91,000 cr Dholera fab just crossed the biggest hurdle: environmental and state water clearance.",
            parts=[
                "Tata's ₹91,000 cr Dholera fab just crossed the biggest hurdle: environmental and state water clearance. With PSMC supplying 28nm process nodes, India's first commercial silicon fab now enters active civil construction."
            ],
            rationale="Focus on unit economics and regulatory clearance significance.",
            claims=[
                DraftClaim(
                    claim="The ₹91,000 crore facility is being built with Taiwan's PSMC.",
                    claim_type="fact",
                    supporting_quote=quote,
                    source_url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
                )
            ],
        ),
        contrarian=SingleAngleCandidate(
            angle="contrarian",
            hook="Clearances are the easy part; the real battlefield for Dholera is reliable ultra-pure water.",
            parts=[
                "Clearances are the easy part; the real battlefield for Dholera is reliable ultra-pure water. While headlines celebrate the ₹91,000 cr sanction, semiconductor fabs consume 20M liters daily in an arid region."
            ],
            rationale="Exposing infrastructure supply chain bottlenecks.",
            claims=[
                DraftClaim(
                    claim="The ₹91,000 crore facility is being built with Taiwan's PSMC.",
                    claim_type="fact",
                    supporting_quote=quote,
                    source_url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
                )
            ],
        ),
        witty=SingleAngleCandidate(
            angle="witty",
            hook="India's semiconductor dream just moved from Powerpoint decks to actual bulldozers.",
            parts=[
                "India's semiconductor dream just moved from Powerpoint decks to actual bulldozers. Tata secured green approval for the ₹91,000 cr Dholera plant. Now we find out if Gujarat's power grid can handle 28nm silicon without blinking."
            ],
            rationale="Witty commentary on the transition from policy hype to real engineering.",
            claims=[
                DraftClaim(
                    claim="The ₹91,000 crore facility is being built with Taiwan's PSMC.",
                    claim_type="fact",
                    supporting_quote=quote,
                    source_url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
                )
            ],
        ),
    )


def build_mock_research_report() -> StructuredResearchReport:
    return StructuredResearchReport(
        topic="Tata Electronics Dholera Fab",
        executive_summary="Tata Electronics received environmental approval for ₹91,000 crore Dholera fab with PSMC.",
        what_happened="Environmental clearance granted for India's first commercial semiconductor fab.",
        key_entities=[
            EntityItem(name="Tata Electronics", entity_type="company", role="lead"),
            EntityItem(name="PSMC", entity_type="company", role="partner"),
            EntityItem(name="Dholera", entity_type="location", role="site"),
        ],
        verified_facts=[
            "Facility estimated at ₹91,000 crore.",
            "Partnership with Taiwan's PSMC.",
            "Located in Dholera, Gujarat.",
        ],
        unresolved_claims=["Targeting 2026 commercial production."],
        conflicting_info=[],
        historical_context="Part of the India Semiconductor Mission ($10B incentive package).",
        stakeholder_analysis=[],
        potential_implications=["Strengthens domestic electronics supply chain."],
        confidence_score=0.92,
    )


@pytest.mark.asyncio
async def test_end_to_end_intelligence_pipeline(
    db: AsyncSession,
    account: Account,
    settings: Settings,
    engine: AsyncEngine,
    seeded_candidate: SourceDocument,
) -> None:
    """Tests full end-to-end autonomous intelligence flow:
    Research -> Event Intelligence -> Memory -> Multi-Angle Generation ->
    Claim Verification -> Critic -> Policy Gate -> Staged in Review Queue.
    """
    mock_llm = FakeIntelligenceLLM(
        research_report=build_mock_research_report(),
        multi_angle=build_mock_multi_angle(),
    )
    embedder = HashingEmbeddings(dimensions=384)
    from app.memory.database import session_scope

    writer = ContentWriter(
        settings,
        mock_llm,  # type: ignore[arg-type]
        session_scope,
        embedder=embedder,
    )

    # Trigger draft creation for candidate
    outcome = await writer.create_original_draft(account.id, [seeded_candidate.id])

    # 1. Verification and Gate Checks
    assert outcome.status == DRAFT_READY
    assert len(outcome.blocked_reasons) == 0
    assert outcome.verification.passed is True
    assert outcome.verification.verified_facts >= 1
    assert outcome.verification.contradicted_claims == 0

    # 2. Database Persistence Checks
    draft = await db.get(Draft, outcome.draft_id)
    assert draft is not None
    assert draft.status == DRAFT_READY
    assert draft.topic == "Tata Electronics Dholera Fab"
    assert draft.tone in ("analytical", "contrarian", "witty")

    # 3. Policy Report contains all 3 alternative angles
    policy = draft.policy_report or {}
    assert "alternative_angles" in policy
    alts = policy["alternative_angles"]
    assert len(alts) == 2  # Champion + 2 alternatives = 3 total angles
    assert "critic" in policy
    assert policy["critic"]["verdict"] == "approved"
    assert policy["critic"]["overall_score"] >= 72.0

    # 4. Research Report persisted in DB
    report_id = policy.get("research_report_id")
    assert report_id is not None
    report = await db.get(ResearchReport, report_id)
    assert report is not None
    assert report.confidence_score >= 0.8
    assert len(report.verified_facts) >= 1

    # 5. Event matched/created and linked to draft
    event_id = policy.get("event_id")
    assert event_id is not None
    event = await db.get(Event, event_id)
    assert event is not None
    assert "Tata Electronics" in event.title

    # 6. EventPostLink recorded
    link = (
        await db.scalars(
            select(EventPostLink).where(EventPostLink.draft_id == draft.id)
        )
    ).first()
    assert link is not None
    assert link.event_id == event.id

    # 7. EditorialMemory recorded for future angle contrast
    ed_mem = (
        await db.scalars(
            select(EditorialMemory).where(EditorialMemory.account_id == account.id)
        )
    ).first()
    assert ed_mem is not None
    assert ed_mem.angle in ("analytical", "contrarian", "witty")

    # 8. EvidenceClaim records stored
    claims = (
        await db.scalars(
            select(EvidenceClaim).where(EvidenceClaim.draft_id == draft.id)
        )
    ).all()
    assert len(claims) >= 1
    assert claims[0].status == "verified"


@pytest.mark.asyncio
async def test_critic_bounded_rewrite_mechanism(
    db: AsyncSession,
    account: Account,
    settings: Settings,
    seeded_candidate: SourceDocument,
) -> None:
    """Verifies that medium-quality drafts undergo at most 2 bounded rewrite attempts."""
    # Build candidate with generic AI phrase to trigger needs_revision
    quote = "The ₹91,000 crore facility is being built in partnership with Taiwan's Powerchip Semiconductor Manufacturing Corp (PSMC)."
    flawed_single = SingleAngleCandidate(
        angle="analytical",
        hook="In a world where chips rule, this is a testament to India's rise.",  # Clichés trigger critic revision
        parts=[
            "In a world where chips rule, this is a testament to India's rise. The ₹91,000 crore facility is being built in partnership with Taiwan's Powerchip Semiconductor Manufacturing Corp (PSMC)."
        ],
        rationale="Analytical test.",
        claims=[
            DraftClaim(
                claim="The ₹91,000 crore facility is being built with Taiwan's PSMC.",
                claim_type="fact",
                supporting_quote=quote,
                source_url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
            )
        ],
    )
    # Rewritten candidate removing clichés
    clean_rewrite = SingleAngleCandidate(
        angle="analytical",
        hook="Tata's ₹91,000 crore Dholera semiconductor fab clears final environmental hurdle.",
        parts=[
            "Tata's ₹91,000 crore Dholera semiconductor fab clears final environmental hurdle. The ₹91,000 crore facility is being built in partnership with Taiwan's Powerchip Semiconductor Manufacturing Corp (PSMC)."
        ],
        rationale="Sharpened factual hook.",
        claims=flawed_single.claims,
    )

    mock_llm = FakeIntelligenceLLM(
        research_report=build_mock_research_report(),
        multi_angle=MultiAngleGenerationResponse(
            topic="Tata Dholera",
            analytical=flawed_single,
            contrarian=flawed_single,
            witty=flawed_single,
        ),
        rewrite_candidate=clean_rewrite,
    )

    engine = EditorialEngine(settings, mock_llm)  # type: ignore[arg-type]
    result = await engine.generate_and_evaluate(
        topic="Tata Dholera",
        documents=[seeded_candidate],
    )

    # Verify that bounded rewrite was executed
    assert len(result.champion.rewrite_history) >= 1
    assert result.champion.rewrite_history[0]["attempt"] == 1
    # Check that final champion score improved and clichés were eliminated
    assert result.champion.critic_score is not None
    assert "generic_ai_phrase" not in result.champion.critic_score.flags
    assert result.champion.critic_score.overall_score >= 72.0


@pytest.mark.asyncio
async def test_contradiction_and_unsupported_claims_block_draft(
    db: AsyncSession,
    account: Account,
    settings: Settings,
    seeded_candidate: SourceDocument,
) -> None:
    """Verifies that contradicted or unsupported material numbers hard-block the draft."""
    mock_llm = FakeIntelligenceLLM(
        research_report=build_mock_research_report(),
        multi_angle=MultiAngleGenerationResponse(
            topic="Tata Dholera",
            analytical=SingleAngleCandidate(
                angle="analytical",
                hook="Tata spent ₹5,00,000 crore on Dholera fab.",  # Unsupported massive number
                parts=["Tata spent ₹5,00,000 crore on Dholera fab."],
                rationale="Wrong claim.",
                claims=[
                    DraftClaim(
                        claim="Tata spent ₹5,00,000 crore on Dholera fab.",
                        claim_type="fact",
                        supporting_quote="Non-existent quote",
                        source_url="https://indianexpress.com/article/business/tata-electronics-dholera-semiconductor-plant",
                    )
                ],
            ),
            contrarian=SingleAngleCandidate(
                angle="contrarian",
                hook="Test hook",
                parts=["Test part"],
                rationale="Test",
                claims=[],
            ),
            witty=SingleAngleCandidate(
                angle="witty",
                hook="Test witty",
                parts=["Test witty part"],
                rationale="Test",
                claims=[],
            ),
        ),
    )
    from app.memory.database import session_scope

    writer = ContentWriter(
        settings,
        mock_llm,  # type: ignore[arg-type]
        session_scope,
        embedder=HashingEmbeddings(dimensions=384),
    )
    outcome = await writer.create_original_draft(account.id, [seeded_candidate.id])

    # Must be BLOCKED
    assert outcome.status == DRAFT_BLOCKED
    assert any("claim" in r or "quote" in r or "unverified" in r for r in outcome.blocked_reasons)
