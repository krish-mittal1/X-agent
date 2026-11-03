"""Integration tests for Event Intelligence & Timeline Memory Service (Phase 2 Part 1).

Tests event matching, duplicate prevention, safe handling of missing embeddings,
and context retrieval using real PostgreSQL with pgvector.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.memory.embeddings import EmbeddingError, HashingEmbeddings
from app.memory.models import Account, EventDevelopment, ResearchReport
from app.research.event_service import EventIntelligenceService, _cosine_similarity


class FailingEmbedder:
    """Simulates an embedding provider failure."""

    model_name = "failing-model"
    dimensions = 384

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("Embedding provider network timeout / unavailable")


class MismatchedDimEmbedder:
    """Returns wrong vector dimension."""

    model_name = "mismatch-model"
    dimensions = 384

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 128 for _ in texts]


async def create_mock_report(
    db: AsyncSession,
    account_id: int,
    topic: str,
    what_happened: str,
    entities: list[dict[str, Any]],
    *,
    confidence: float = 0.85,
) -> ResearchReport:
    report = ResearchReport(
        account_id=account_id,
        topic=topic,
        executive_summary=what_happened,
        what_happened=what_happened,
        key_entities=entities,
        verified_facts=[f"Fact: {what_happened[:50]}"],
        unresolved_claims=[],
        conflicting_info=[],
        historical_context="Context",
        confidence_score=confidence,
        status="completed",
        metadata_={},
    )
    db.add(report)
    await db.commit()
    return report


@pytest.mark.asyncio
async def test_cosine_similarity_edge_cases() -> None:
    # 1. Missing or empty vectors return 0.0
    assert _cosine_similarity(None, [0.1, 0.2]) == 0.0
    assert _cosine_similarity([0.1, 0.2], None) == 0.0
    assert _cosine_similarity([], []) == 0.0
    # 2. Dimension mismatch returns 0.0
    assert _cosine_similarity([0.1, 0.2], [0.1, 0.2, 0.3]) == 0.0
    # 3. Zero magnitude returns 0.0
    assert _cosine_similarity([0.0, 0.0], [0.1, 0.2]) == 0.0
    # 4. Valid vectors return correct cosine
    sim = _cosine_similarity([1.0, 0.0], [1.0, 0.0])
    assert sim is not None and abs(sim - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_event_matching_and_separate_events(
    db: AsyncSession, account: Account, settings: Settings
) -> None:
    embedder = HashingEmbeddings(dimensions=384)
    service = EventIntelligenceService(settings, embedder=embedder)

    # 1. Incept initial event: Tata Electronics Dholera semiconductor fab
    report1 = await create_mock_report(
        db,
        account.id,
        topic="Tata Electronics begins ground work on ₹91,000 crore Dholera semiconductor fab",
        what_happened="Tata Electronics in partnership with PSMC begins construction on India's first commercial semiconductor fabrication facility in Dholera, Gujarat.",
        entities=[
            {"name": "Tata Electronics", "entity_type": "company", "role": "builder"},
            {"name": "PSMC", "entity_type": "company", "role": "partner"},
            {"name": "Dholera", "entity_type": "location", "role": "site"},
        ],
    )
    event1, is_new1, delta1 = await service.match_or_create_event(db, account.id, report1)
    assert is_new1 is True
    assert event1.id is not None
    assert event1.title.startswith("Tata Electronics begins ground work")

    # 2. Follow-up Tata Electronics semiconductor development: should MATCH existing event
    report2 = await create_mock_report(
        db,
        account.id,
        topic="Tata Electronics signs equipment supply deal for Dholera chip foundry",
        what_happened="Tata Electronics signs major equipment purchase agreements with international tooling vendors for the upcoming Dholera semiconductor fab.",
        entities=[
            {"name": "Tata Electronics", "entity_type": "company", "role": "buyer"},
            {"name": "Dholera", "entity_type": "location", "role": "site"},
        ],
    )
    event2, is_new2, delta2 = await service.match_or_create_event(db, account.id, report2)
    assert is_new2 is False, f"Expected match to event {event1.id}, but created new event with note: {delta2}"
    assert event2.id == event1.id
    assert "Continuation of existing event" in delta2

    # Verify event developments in DB
    devs = (
        await db.scalars(
            select(EventDevelopment).where(EventDevelopment.event_id == event1.id)
        )
    ).all()
    assert len(devs) == 2  # Inception + 1 continuation

    # 3. Repeated reporting of the SAME development does not create duplicate events or developments
    event2_dup, is_new_dup, delta_dup = await service.match_or_create_event(db, account.id, report2)
    assert is_new_dup is False
    assert event2_dup.id == event1.id
    assert "Duplicate reporting acknowledged" in delta_dup

    devs_dup = (
        await db.scalars(
            select(EventDevelopment).where(EventDevelopment.event_id == event1.id)
        )
    ).all()
    assert len(devs_dup) == 2  # Still 2, no duplicate development added

    # 4. Unrelated ISRO development creates a SEPARATE event
    report_isro = await create_mock_report(
        db,
        account.id,
        topic="ISRO successfully launches PSLV-C58 XPoSat space observatory mission",
        what_happened="ISRO launched its polarimetry mission XPoSat from Sriharikota to study astronomical X-ray sources in deep space.",
        entities=[
            {"name": "ISRO", "entity_type": "institution", "role": "space agency"},
            {"name": "PSLV-C58", "entity_type": "rocket", "role": "vehicle"},
            {"name": "Sriharikota", "entity_type": "location", "role": "launch site"},
        ],
    )
    event_isro, is_new_isro, delta_isro = await service.match_or_create_event(
        db, account.id, report_isro
    )
    assert is_new_isro is True
    assert event_isro.id != event1.id
    assert "New event created" in delta_isro


@pytest.mark.asyncio
async def test_unrelated_event_with_similar_entity_not_merged(
    db: AsyncSession, account: Account, settings: Settings
) -> None:
    """Tata Motors car sales should NOT merge with Tata Electronics semiconductor plant."""
    embedder = HashingEmbeddings(dimensions=384)
    service = EventIntelligenceService(settings, embedder=embedder)

    # 1. Semiconductor event
    report_semi = await create_mock_report(
        db,
        account.id,
        topic="Tata Electronics accelerates Dholera semiconductor wafer fab timeline",
        what_happened="Tata Electronics plans to produce first silicon microchips in Dholera by late 2026.",
        entities=[
            {"name": "Tata", "entity_type": "group", "role": "conglomerate"},
            {"name": "Dholera", "entity_type": "location", "role": "site"},
        ],
    )
    event_semi, is_new_semi, _ = await service.match_or_create_event(
        db, account.id, report_semi
    )
    assert is_new_semi is True

    # 2. Car sales event with generic 'Tata' entity
    report_motors = await create_mock_report(
        db,
        account.id,
        topic="Tata Motors reports 15% increase in domestic passenger electric vehicle sales",
        what_happened="Tata Motors announced quarterly EV retail numbers showing sustained consumer demand for Nexon EV.",
        entities=[
            {"name": "Tata", "entity_type": "group", "role": "conglomerate"},
            {"name": "Nexon EV", "entity_type": "product", "role": "car"},
        ],
    )
    event_motors, is_new_motors, delta = await service.match_or_create_event(
        db, account.id, report_motors
    )
    # Must NOT merge!
    assert is_new_motors is True, f"Unrelated vehicle event falsely merged into semiconductor event: {delta}"
    assert event_motors.id != event_semi.id


@pytest.mark.asyncio
async def test_embedding_provider_failure_handled_safely(
    db: AsyncSession, account: Account, settings: Settings
) -> None:
    """When embedding provider fails or raises exceptions, matching degrades safely without crashing."""
    service = EventIntelligenceService(settings, embedder=FailingEmbedder())  # type: ignore[arg-type]

    # Verify get_embedding handles exception safely and returns None
    vec = await service.get_embedding("Testing failure")
    assert vec is None

    # Matching with failing embedder works safely without throwing unhandled exceptions
    report = await create_mock_report(
        db,
        account.id,
        topic="Reserve Bank of India keeps repo rate unchanged at 6.5%",
        what_happened="RBI Monetary Policy Committee decided to maintain repo rate citing food inflation concerns.",
        entities=[
            {"name": "RBI", "entity_type": "regulator", "role": "central bank"},
            {"name": "MPC", "entity_type": "committee", "role": "policy body"},
        ],
    )
    event, is_new, delta = await service.match_or_create_event(db, account.id, report)
    assert is_new is True
    assert event.id is not None
    assert event.embedding is None  # Recorded safely as None without crash


@pytest.mark.asyncio
async def test_event_memory_context_retrieval(
    db: AsyncSession, account: Account, settings: Settings
) -> None:
    embedder = HashingEmbeddings(dimensions=384)
    service = EventIntelligenceService(settings, embedder=embedder)

    report = await create_mock_report(
        db,
        account.id,
        topic="SEBI introduces tighter disclosure rules for algorithmic trading firms",
        what_happened="SEBI issued a circular mandating audit logs for automated algorithmic trading APIs.",
        entities=[
            {"name": "SEBI", "entity_type": "regulator", "role": "market regulator"},
        ],
    )
    event, _, _ = await service.match_or_create_event(db, account.id, report)

    # Record an editorial take memory
    await service.record_editorial_memory(
        db,
        account.id,
        topic="SEBI Algo Trading",
        angle="analytical",
        format_name="analysis",
        hook_type="provocative_question",
        writing_notes="Focus on latency arbitrage",
    )

    # Retrieve context
    mem_ctx = await service.retrieve_event_memory_context(db, account.id, event.id)
    assert mem_ctx["event_id"] == event.id
    assert mem_ctx["event_title"].startswith("SEBI introduces")
    assert len(mem_ctx["timeline"]) >= 1
    assert len(mem_ctx["previous_angles"]) >= 1

    prompt_block = service.format_memory_prompt_block(mem_ctx)
    assert "--- EVENT INTELLIGENCE & TIMELINE MEMORY ---" in prompt_block
    assert "SEBI" in prompt_block
    assert "analysis (analytical)" in prompt_block
