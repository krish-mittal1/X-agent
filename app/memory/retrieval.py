"""Hybrid retrieval: pgvector similarity + Postgres full-text + priors.

Ranking:
  1. Vector search and full-text search each produce a ranked list.
  2. Reciprocal Rank Fusion combines them: sum(1 / (k + rank)) per list.
  3. Small multiplicative priors apply recency decay and source reliability, and a
     conversation-match boost when retrieving inside an explicit conversation.
     These priors break ties; they do not dominate semantic relevance.

Retrieval explanations are logged at DEBUG and returned in the result for
inspection. No secrets or private account data are included.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingError, EmbeddingProvider
from app.memory.models import MemoryRecord
from app.memory.vector_store import MemoryHit, MemoryVectorStore

logger = get_logger(__name__)


@dataclass
class RetrievedMemory:
    record: MemoryRecord
    score: float
    signals: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""


@dataclass
class RetrievalResult:
    memories: list[RetrievedMemory]
    query: str
    used_vector: bool
    used_fts: bool
    degraded_reason: str | None = None

    def as_prompt_context(self, max_chars_per_memory: int = 1200) -> list[dict[str, Any]]:
        return [
            {
                "category": item.record.category,
                "record_type": item.record.record_type,
                "summary": item.record.summary,
                "content": item.record.content[:max_chars_per_memory],
                "source_url": item.record.provenance.get("source_url"),
                "reliability": item.record.provenance.get("reliability"),
                "created_at": item.record.created_at.isoformat(),
            }
            for item in self.memories
        ]


def _recency_factor(created_at: datetime, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    age = max(0.0, (datetime.now(UTC) - created_at).total_seconds() / 86400.0)
    return math.exp(-age / half_life_days)


class HybridRetriever:
    def __init__(self, settings: Settings, embedder: EmbeddingProvider | None) -> None:
        self.settings = settings
        self.embedder = embedder
        self.store = MemoryVectorStore(settings)

    async def _embed_query(self, query: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            vectors = await self.embedder.embed([query])
            return vectors[0]
        except EmbeddingError as exc:
            logger.warning("query embedding failed; falling back to full-text",
                           extra={"extra_fields": {"error": str(exc)[:200]}})
            return None

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        query: str,
        categories: Sequence[str] | None = None,
        conversation_thread_id: int | None = None,
        limit: int | None = None,
    ) -> RetrievalResult:
        query = query.strip()
        if not query:
            return RetrievalResult([], query, False, False, "empty query")
        max_results = limit or self.settings.retrieval_max_results
        vector_hits: list[MemoryHit] = []
        fts_hits: list[MemoryHit] = []
        degraded: str | None = None
        used_vector = False

        if self.embedder is not None:
            vector = await self._embed_query(query)
            if vector is not None:
                vector_hits = await self.store.vector_search(
                    session,
                    account_id=account_id,
                    query_vector=vector,
                    model_name=self.embedder.model_name,
                    limit=self.settings.retrieval_vector_limit,
                    categories=categories,
                    conversation_thread_id=conversation_thread_id,
                    include_global=True,
                    min_similarity=self.settings.retrieval_min_similarity,
                )
                used_vector = True
            else:
                degraded = "vector unavailable; full-text only"
        else:
            degraded = "no embedding provider; full-text only"

        fts_hits = await self.store.fts_search(
            session,
            account_id=account_id,
            query=query,
            limit=self.settings.retrieval_fts_limit,
            categories=categories,
            conversation_thread_id=conversation_thread_id,
            include_global=True,
        )

        scores: dict[int, dict[str, Any]] = {}
        for rank, hit in enumerate(vector_hits, start=1):
            entry = scores.setdefault(hit.record.id, {"record": hit.record, "vector_rank": rank,
                                                      "similarity": hit.similarity, "fts_rank": None})
            entry["vector_rank"] = rank
            entry["similarity"] = hit.similarity
        for rank, hit in enumerate(fts_hits, start=1):
            entry = scores.setdefault(hit.record.id, {"record": hit.record, "vector_rank": None,
                                                      "similarity": None, "fts_rank": rank})
            entry["fts_rank"] = rank
            entry["fts_score"] = hit.rank_score

        k = self.settings.retrieval_rrf_k
        results: list[RetrievedMemory] = []
        for entry in scores.values():
            record: MemoryRecord = entry["record"]
            rrf = 0.0
            if entry["vector_rank"] is not None:
                rrf += 1.0 / (k + entry["vector_rank"])
            if entry["fts_rank"] is not None:
                rrf += 1.0 / (k + entry["fts_rank"])
            recency = _recency_factor(record.valid_from, self.settings.retrieval_recency_half_life_days)
            reliability = float(record.provenance.get("reliability", 0.5) or 0.5)
            thread_match = (
                conversation_thread_id is not None
                and record.conversation_thread_id == conversation_thread_id
            )
            prior = (0.9 + 0.1 * recency) * (0.97 + 0.03 * reliability)
            if thread_match:
                prior *= 1.05
            score = rrf * prior
            parts = []
            if entry["vector_rank"] is not None:
                parts.append(f"vector#{entry['vector_rank']} sim={entry['similarity']:.3f}")
            if entry["fts_rank"] is not None:
                parts.append(f"fts#{entry['fts_rank']}")
            parts.append(f"recency={recency:.2f} reliability={reliability:.2f}")
            if thread_match:
                parts.append("thread_match")
            results.append(
                RetrievedMemory(
                    record=record,
                    score=score,
                    signals={
                        "vector_rank": entry["vector_rank"],
                        "fts_rank": entry["fts_rank"],
                        "similarity": entry["similarity"],
                        "recency": recency,
                        "reliability": reliability,
                        "thread_match": thread_match,
                        "rrf": rrf,
                    },
                    explanation="; ".join(parts),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        results = results[:max_results]
        logger.debug(
            "retrieval",
            extra={
                "extra_fields": {
                    "account_id": account_id,
                    "query": query[:120],
                    "results": [
                        {"id": item.record.id, "why": item.explanation} for item in results
                    ],
                    "degraded": degraded,
                }
            },
        )
        return RetrievalResult(results, query, used_vector, bool(fts_hits), degraded)

    async def novelty_score(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        text: str,
        lookback_days: int = 45,
    ) -> float | None:
        """1 - max cosine similarity to recent published posts; None if unavailable."""
        if self.embedder is None:
            return None
        try:
            vector = (await self.embedder.embed([text]))[0]
        except EmbeddingError:
            return None
        since = datetime.now(UTC) - timedelta(days=lookback_days)
        similarity = await self.store.closest_recent_post_similarity(
            session,
            account_id=account_id,
            query_vector=vector,
            model_name=self.embedder.model_name,
            since=since,
        )
        return max(0.0, min(1.0, 1.0 - similarity))
