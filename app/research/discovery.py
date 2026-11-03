"""Discovery pipeline: fetch sources, deduplicate, index memory, score candidates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.persona import load_persona
from app.config import Settings
from app.ingestion.base import SourceAdapter
from app.ingestion.github import GitHubAdapter
from app.ingestion.rss import RssAdapter
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider
from app.memory.repositories import SourceRepository
from app.memory.retrieval import HybridRetriever
from app.memory.vector_store import MemoryVectorStore
from app.research.scoring import CandidateScorer
from app.schemas.research import DiscoveryStats

logger = get_logger(__name__)


class DiscoveryService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        adapters: Sequence[SourceAdapter] | None = None,
        embedder: EmbeddingProvider | None = None,
        scorer: CandidateScorer | None = None,
        classifier=None,  # CandidateClassifier
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._adapters = list(adapters) if adapters is not None else None
        self.embedder = embedder
        self.scorer = scorer or CandidateScorer(settings)
        self.classifier = classifier
        self.store = MemoryVectorStore(settings)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _build_adapters(self, client: httpx.AsyncClient) -> list[SourceAdapter]:
        adapters: list[SourceAdapter] = []
        if self.settings.feed_urls:
            adapters.append(RssAdapter(self.settings, client))
        if self.settings.repositories or self.settings.github_queries:
            adapters.append(GitHubAdapter(self.settings, client))
        return adapters

    async def run(
        self,
        account_id: int,
        *,
        source_filter: set[str] | None = None,
        ingest: bool = True,
        score: bool = True,
    ) -> DiscoveryStats:
        stats = DiscoveryStats()
        if not self.settings.discovery_enabled:
            stats.errors.append("discovery disabled by configuration (DISCOVERY_ENABLED=false)")
            return stats

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            headers={"User-Agent": self.settings.http_user_agent},
            follow_redirects=False,
        )
        try:
            if ingest:
                adapters = (
                    self._adapters
                    if self._adapters is not None
                    else self._build_adapters(client)
                )
                if source_filter is not None:
                    adapters = [a for a in adapters if a.name in source_filter]
                if not adapters:
                    stats.errors.append(
                        "no research sources configured (RSS_FEEDS / GITHUB_REPOSITORIES)"
                    )
                else:
                    await self._ingest(account_id, adapters, stats)
        finally:
            await client.aclose()

        if score:
            await self._score(account_id, stats)
        return stats

    async def _ingest(
        self, account_id: int, adapters: Sequence[SourceAdapter], stats: DiscoveryStats
    ) -> None:
        async with self.session_factory() as session:
            for adapter in adapters:
                try:
                    result = await adapter.fetch()
                except Exception as exc:  # adapter failure must not stop discovery
                    logger.exception("adapter failed", extra={"extra_fields": {"adapter": adapter.name}})
                    stats.ingested.errors.append(f"{adapter.name}: {type(exc).__name__}: {exc}")
                    continue
                stats.ingested.errors.extend(result.errors)
                stats.ingested.rejected += result.rejected
                stats.ingested.fetched += len(result.items)
                for item in result.items:
                    try:
                        document, created = await SourceRepository.store_item(
                            session, account_id, item
                        )
                    except Exception as exc:  # malformed item / DB constraint
                        logger.warning(
                            "failed to store source item",
                            extra={"extra_fields": {"url": item.url, "error": str(exc)[:200]}},
                        )
                        stats.ingested.errors.append(f"store failed for {item.url}: {exc}")
                        continue
                    if not created or document is None:
                        stats.ingested.duplicates += 1
                        continue
                    stats.ingested.stored += 1
                    if self.embedder is not None:
                        try:
                            await self.store.index_source_document(
                                session, document, self.embedder
                            )
                        except Exception as exc:
                            logger.warning(
                                "source indexing failed",
                                extra={"extra_fields": {"document_id": document.id,
                                                        "error": str(exc)[:200]}},
                            )
                            stats.ingested.errors.append(
                                f"indexing failed for {item.url}: {type(exc).__name__}"
                            )
            await session.commit()

    async def _score(self, account_id: int, stats: DiscoveryStats) -> None:
        async with self.session_factory() as session:
            persona = await load_persona(session, account_id)
            cutoff = self.clock() - timedelta(hours=self.settings.freshness_window_hours)
            documents = await SourceRepository.list_unscored(
                session, account_id, cutoff=cutoff, limit=50
            )
            retriever = HybridRetriever(self.settings, self.embedder)
            refined = 0
            for document in documents:
                try:
                    novelty = await retriever.novelty_score(
                        session,
                        account_id=account_id,
                        text=f"{document.title}. {document.excerpt}",
                    )
                    score = self.scorer.score_document(
                        document,
                        interests=persona.interests,
                        novelty=novelty,
                        now=self.clock(),
                    )
                    if (
                        self.classifier is not None
                        and refined < 5
                        and score.score >= self.settings.min_candidate_score * 0.75
                    ):
                        classification = await self.classifier.classify(document)
                        if classification is not None:
                            refined += 1
                            stats.llm_refined += 1
                            score = self.scorer.score_document(
                                document,
                                interests=persona.interests,
                                novelty=novelty,
                                now=self.clock(),
                                refinement=classification,
                            )
                    await SourceRepository.apply_score(session, document, score)
                    stats.scored += 1
                    if score.score >= self.settings.min_candidate_score:
                        stats.above_threshold += 1
                except Exception as exc:
                    logger.exception("scoring failed",
                                     extra={"extra_fields": {"document_id": document.id}})
                    stats.errors.append(f"scoring failed for {document.id}: {type(exc).__name__}")
            await session.commit()


async def run_discovery(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> DiscoveryStats:
    from app.memory.embeddings import build_embedding_provider

    try:
        embedder = build_embedding_provider(settings)
    except Exception as exc:
        logger.error("embedding provider unavailable; discovery continues without vectors",
                     extra={"extra_fields": {"error": str(exc)[:300]}})
        embedder = None
    service = DiscoveryService(settings, session_factory, embedder=embedder)
    return await service.run(account_id)


__all__ = ["DiscoveryService", "run_discovery"]
