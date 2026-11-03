from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.config import Settings
from app.ingestion.base import AdapterResult
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import MemoryRecord, SourceDocument
from app.research.discovery import DiscoveryService
from app.schemas.research import CandidateClassification, IngestedItem


class FakeAdapter:
    name = "fake"

    def __init__(self, items: list[IngestedItem]) -> None:
        self.items = items

    async def fetch(self) -> AdapterResult:
        return AdapterResult(items=list(self.items))


def item(**overrides: Any) -> IngestedItem:
    values = {
        "source_type": "rss",
        "source_identifier": "https://example.com/feed",
        "url": "https://example.com/post",
        "title": "Faster inference with paged KV cache",
        "content": "Benchmark: 1,200 tokens per second on one CPU core using a paged KV cache. "
        "The release deprecates the old scheduler and documents a migration path for developers.",
        "excerpt": "Benchmark and migration notes for a new inference scheduler.",
        "published_at": datetime.now(UTC),
        "reliability_score": 0.75,
        "metadata": {"is_primary_source": True},
    }
    values.update(overrides)
    return IngestedItem(**values)


def make_service(settings: Settings, adapter: FakeAdapter) -> DiscoveryService:
    return DiscoveryService(
        settings,
        db_module.get_session_factory(),
        adapters=[adapter],
        embedder=HashingEmbeddings(384),
    )


async def test_discovery_stores_scores_and_indexes(settings, engine, account) -> None:
    service = make_service(settings, FakeAdapter([item()]))
    stats = await service.run(account.id)
    assert stats.ingested.fetched == 1
    assert stats.ingested.stored == 1
    assert stats.ingested.duplicates == 0
    assert stats.scored == 1
    assert stats.above_threshold >= 0

    async with session_scope() as session:
        document = await session.scalar(select(SourceDocument))
        assert document is not None
        assert document.score_breakdown is not None
        assert 0 <= (document.candidate_score or 0) <= 1
        assert "signals" in document.score_breakdown
        assert document.scored_at is not None
        memory_count = await session.scalar(
            select(func.count(MemoryRecord.id)).where(MemoryRecord.source_document_id == document.id)
        )
    assert memory_count and memory_count >= 1


async def test_discovery_deduplicates_on_second_run(settings, engine, account) -> None:
    service = make_service(settings, FakeAdapter([item()]))
    await service.run(account.id)
    second = await make_service(settings, FakeAdapter([item()])).run(account.id)
    assert second.ingested.stored == 0
    assert second.ingested.duplicates == 1
    assert second.scored == 0
    async with session_scope() as session:
        count = await session.scalar(select(func.count(SourceDocument.id)))
    assert count == 1


async def test_discovery_respects_kill_switch(settings, engine, account) -> None:
    disabled = Settings(**{**settings.model_dump(), "discovery_enabled": False})
    stats = await make_service(disabled, FakeAdapter([item()])).run(account.id)
    assert stats.ingested.fetched == 0
    assert any("disabled" in error for error in stats.errors)


class FakeClassifier:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, document) -> CandidateClassification:  # type: ignore[no-untyped-def]
        self.calls += 1
        return CandidateClassification(
            technical_relevance=0.9,
            audience_usefulness=0.8,
            reasoning="Concrete benchmark and migration notes.",
            is_marketing_fluff=False,
        )


async def test_discovery_applies_llm_classification(settings, engine, account) -> None:
    classifier = FakeClassifier()
    tuned = settings.model_copy(update={"min_candidate_score": 0.0})
    service = DiscoveryService(
        tuned,
        db_module.get_session_factory(),
        adapters=[FakeAdapter([item()])],
        embedder=HashingEmbeddings(384),
        classifier=classifier,
    )
    stats = await service.run(account.id)
    assert stats.llm_refined == 1
    assert classifier.calls == 1
    async with session_scope() as session:
        document = await session.scalar(select(SourceDocument))
    assert document is not None
    assert document.score_breakdown is not None
    assert document.score_breakdown["llm_refinement"]["reasoning"].startswith("Concrete")


async def test_discovery_survives_adapter_exception(settings, engine, account) -> None:
    class BrokenAdapter:
        name = "broken"

        async def fetch(self) -> AdapterResult:
            raise RuntimeError("upstream exploded")

    service = DiscoveryService(
        settings,
        db_module.get_session_factory(),
        adapters=[BrokenAdapter()],
        embedder=None,
    )
    stats = await service.run(account.id)
    assert stats.ingested.fetched == 0
    assert any("upstream exploded" in error for error in stats.ingested.errors)
