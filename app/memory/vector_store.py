"""pgvector persistence and search primitives.

Every read is account-scoped. Conversation-scoped reads include global memories
and the requested thread only, never another thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_config import get_logger
from app.memory.chunking import chunk_text
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import (
    MEMORY_CONVERSATION,
    MEMORY_EPISODIC,
    MEMORY_SEMANTIC,
    MemoryEmbedding,
    MemoryRecord,
    SourceDocument,
)
from app.net import content_hash

logger = get_logger(__name__)


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    similarity: float | None = None
    rank_score: float | None = None


@dataclass(frozen=True)
class IndexResult:
    chunks: int
    embedded: int


class MemoryVectorStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------
    async def _existing_records(
        self, session: AsyncSession, account_id: int, hashes: Sequence[str]
    ) -> dict[str, MemoryRecord]:
        if not hashes:
            return {}
        rows = await session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.account_id == account_id, MemoryRecord.content_hash.in_(hashes)
            )
        )
        return {row.content_hash: row for row in rows}

    async def add_memory(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        category: str,
        record_type: str,
        content: str,
        summary: str | None = None,
        source_document_id: int | None = None,
        conversation_thread_id: int | None = None,
        post_id: int | None = None,
        confidence: float = 0.5,
        provenance: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        expires_at: datetime | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        embed_text: str | None = None,
        chunk: bool = False,
    ) -> tuple[MemoryRecord, int]:
        """Add a memory (optionally chunked) and its embeddings.

        Returns (record, number_of_embeddings_written). Idempotent on content hash.
        """
        texts = (
            [c.text for c in chunk_text(
                content,
                max_words=self.settings.embedding_chunk_words,
                overlap_words=self.settings.embedding_chunk_overlap,
                min_words=self.settings.embedding_min_chunk_words,
            )]
            if chunk
            else [content]
        )
        if not texts:
            raise ValueError("cannot store empty memory")
        primary: MemoryRecord | None = None
        embedded = 0
        for index, text in enumerate(texts):
            digest = content_hash(f"{account_id}:{category}:{record_type}:{index}:{text}")
            record = (await self._existing_records(session, account_id, [digest])).get(digest)
            if record is None:
                record = MemoryRecord(
                    account_id=account_id,
                    category=category,
                    record_type=record_type,
                    content=text,
                    summary=summary,
                    source_document_id=source_document_id,
                    conversation_thread_id=conversation_thread_id,
                    post_id=post_id,
                    confidence=confidence,
                    provenance=provenance or {},
                    content_hash=digest,
                    valid_from=valid_from or datetime.now(UTC),
                    expires_at=expires_at,
                )
                try:
                    async with session.begin_nested():
                        session.add(record)
                        await session.flush()
                except IntegrityError:
                    record = (await self._existing_records(session, account_id, [digest])).get(digest)
                    if record is None:
                        continue
            if embedding_provider is not None:
                embedded += await self._ensure_embedding(
                    session, record, text, embedding_provider, embed_text=embed_text
                )
            if primary is None:
                primary = record
        if primary is None:
            raise RuntimeError("failed to persist memory record")
        return primary, embedded

    async def _ensure_embedding(
        self,
        session: AsyncSession,
        record: MemoryRecord,
        text: str,
        provider: EmbeddingProvider,
        *,
        embed_text: str | None = None,
    ) -> int:
        existing = await session.scalar(
            select(func.count(MemoryEmbedding.id)).where(
                MemoryEmbedding.memory_record_id == record.id,
                MemoryEmbedding.embedding_model == provider.model_name,
                MemoryEmbedding.chunk_index == 0,
            )
        )
        if existing:
            return 0
        payload = embed_text or text
        vectors = await provider.embed([payload])
        vector = vectors[0]
        if len(vector) != provider.dimensions:
            raise ValueError(
                f"embedder returned {len(vector)} dimensions, expected {provider.dimensions}"
            )
        try:
            async with session.begin_nested():
                session.add(
                    MemoryEmbedding(
                        memory_record_id=record.id,
                        embedding_model=provider.model_name,
                        dimensions=provider.dimensions,
                        chunk_index=0,
                        chunk_text=text,
                        embedding=vector,
                    )
                )
                await session.flush()
        except IntegrityError:
            return 0
        return 1

    async def index_source_document(
        self,
        session: AsyncSession,
        document: SourceDocument,
        provider: EmbeddingProvider,
    ) -> IndexResult:
        """Chunk a source document into semantic memory and embed each chunk."""
        chunks = chunk_text(
            f"{document.title}\n{document.content}",
            max_words=self.settings.embedding_chunk_words,
            overlap_words=self.settings.embedding_chunk_overlap,
            min_words=self.settings.embedding_min_chunk_words,
        )[: self.settings.memory_max_chunks_per_document]
        if not chunks:
            return IndexResult(chunks=0, embedded=0)

        existing = await session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.account_id == document.account_id,
                MemoryRecord.source_document_id == document.id,
                MemoryRecord.record_type == "source_chunk",
            )
        )
        by_hash = {row.content_hash: row for row in existing}
        records: list[MemoryRecord] = []
        for chunk in chunks:
            digest = content_hash(
                f"{document.account_id}:{MEMORY_SEMANTIC}:source_chunk:{document.id}:{chunk.index}:{chunk.text}"
            )
            record = by_hash.get(digest)
            if record is None:
                record = MemoryRecord(
                    account_id=document.account_id,
                    category=MEMORY_SEMANTIC,
                    record_type="source_chunk",
                    content=chunk.text,
                    summary=document.title,
                    source_document_id=document.id,
                    confidence=document.reliability_score,
                    provenance={
                        "source_url": document.url,
                        "title": document.title,
                        "source_type": document.source_type,
                        "published_at": document.published_at.isoformat() if document.published_at else None,
                        "retrieved_at": document.retrieved_at.isoformat(),
                        "reliability": document.reliability_score,
                        "is_primary_source": bool(document.metadata_.get("is_primary_source")),
                        "chunk_index": chunk.index,
                        "fetch_status": document.fetch_status,
                    },
                    content_hash=digest,
                    valid_from=document.published_at or document.retrieved_at,
                )
                try:
                    async with session.begin_nested():
                        session.add(record)
                        await session.flush()
                except IntegrityError:
                    continue
            records.append(record)
        await session.flush()

        # Batch-embed only the chunks that do not yet have a vector for this model.
        record_ids = [record.id for record in records]
        already = set(
            (
                await session.scalars(
                    select(MemoryEmbedding.memory_record_id).where(
                        MemoryEmbedding.memory_record_id.in_(record_ids),
                        MemoryEmbedding.embedding_model == provider.model_name,
                        MemoryEmbedding.chunk_index == 0,
                    )
                )
            ).all()
        )
        pending = [record for record in records if record.id not in already]
        embedded = 0
        batch_size = max(1, self.settings.embedding_batch_size)
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            vectors = await provider.embed([record.content for record in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedding provider returned the wrong number of vectors")
            for record, vector in zip(batch, vectors, strict=True):
                if len(vector) != provider.dimensions:
                    raise ValueError(
                        f"embedder returned {len(vector)} dims, expected {provider.dimensions}"
                    )
                try:
                    async with session.begin_nested():
                        session.add(
                            MemoryEmbedding(
                                memory_record_id=record.id,
                                embedding_model=provider.model_name,
                                dimensions=provider.dimensions,
                                chunk_index=0,
                                chunk_text=record.content,
                                embedding=vector,
                            )
                        )
                        await session.flush()
                except IntegrityError:
                    continue
                embedded += 1
        return IndexResult(chunks=len(records), embedded=embedded)

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------
    def _scope_conditions(
        self, account_id: int, conversation_thread_id: int | None, include_global: bool
    ) -> list[Any]:
        now = datetime.now(UTC)
        conditions: list[Any] = [
            MemoryRecord.account_id == account_id,
            MemoryRecord.invalidated_at.is_(None),
            or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
        ]
        if conversation_thread_id is not None:
            thread_filter = MemoryRecord.conversation_thread_id == conversation_thread_id
            if include_global:
                thread_filter = or_(thread_filter, MemoryRecord.conversation_thread_id.is_(None))
            conditions.append(thread_filter)
        else:
            conditions.append(MemoryRecord.conversation_thread_id.is_(None))
        return conditions

    async def vector_search(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        query_vector: list[float],
        model_name: str,
        limit: int,
        categories: Sequence[str] | None = None,
        conversation_thread_id: int | None = None,
        include_global: bool = True,
        min_similarity: float = 0.0,
    ) -> list[MemoryHit]:
        if len(query_vector) != self.settings.embedding_dimensions:
            raise ValueError(
                f"query vector has {len(query_vector)} dims, expected "
                f"{self.settings.embedding_dimensions}"
            )
        distance = MemoryEmbedding.embedding.cosine_distance(query_vector).label("distance")
        conditions = self._scope_conditions(account_id, conversation_thread_id, include_global)
        conditions.append(MemoryEmbedding.embedding_model == model_name)
        if categories:
            conditions.append(MemoryRecord.category.in_(categories))
        stmt = (
            select(MemoryRecord, distance)
            .join(MemoryEmbedding, MemoryEmbedding.memory_record_id == MemoryRecord.id)
            .where(*conditions)
            .order_by(distance)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        best: dict[int, MemoryHit] = {}
        for record, dist in rows:
            similarity = 1.0 - float(dist)
            if similarity < min_similarity:
                continue
            previous = best.get(record.id)
            if previous is None or (previous.similarity or 0) < similarity:
                best[record.id] = MemoryHit(record=record, similarity=similarity)
        return sorted(best.values(), key=lambda hit: hit.similarity or 0, reverse=True)

    async def fts_search(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        query: str,
        limit: int,
        categories: Sequence[str] | None = None,
        conversation_thread_id: int | None = None,
        include_global: bool = True,
    ) -> list[MemoryHit]:
        query = query.strip()
        if not query:
            return []
        tsquery = func.websearch_to_tsquery("simple", query)
        rank = func.ts_rank_cd(MemoryRecord.search_vector, tsquery).label("rank")
        conditions = self._scope_conditions(account_id, conversation_thread_id, include_global)
        conditions.append(MemoryRecord.search_vector.op("@@")(tsquery))
        if categories:
            conditions.append(MemoryRecord.category.in_(categories))
        stmt = (
            select(MemoryRecord, rank)
            .where(*conditions)
            .order_by(rank.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [
            MemoryHit(record=record, rank_score=float(score))
            for record, score in rows
            if score and float(score) > 0
        ]

    async def closest_recent_post_similarity(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        query_vector: list[float],
        model_name: str,
        since: datetime,
    ) -> float:
        distance = MemoryEmbedding.embedding.cosine_distance(query_vector)
        conditions = [
            MemoryRecord.account_id == account_id,
            MemoryRecord.category == MEMORY_EPISODIC,
            MemoryRecord.record_type == "published_post",
            MemoryRecord.invalidated_at.is_(None),
            MemoryRecord.created_at >= since,
            MemoryEmbedding.embedding_model == model_name,
        ]
        stmt = (
            select(distance)
            .select_from(MemoryEmbedding)
            .join(MemoryRecord, MemoryEmbedding.memory_record_id == MemoryRecord.id)
            .where(*conditions)
            .order_by(distance)
            .limit(1)
        )
        result = await session.scalar(stmt)
        if result is None:
            return 0.0
        return max(0.0, min(1.0, 1.0 - float(result)))

    async def recent_conversation_memories(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        conversation_thread_id: int,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        stmt = (
            select(MemoryRecord)
            .where(
                MemoryRecord.account_id == account_id,
                MemoryRecord.conversation_thread_id == conversation_thread_id,
                MemoryRecord.category == MEMORY_CONVERSATION,
                MemoryRecord.invalidated_at.is_(None),
            )
            .order_by(MemoryRecord.created_at.desc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    async def expire_conversation_memory(
        self, session: AsyncSession, account_id: int, older_than_days: int
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        stmt = (
            select(MemoryRecord)
            .where(
                MemoryRecord.account_id == account_id,
                MemoryRecord.category == MEMORY_CONVERSATION,
                MemoryRecord.created_at < cutoff,
                MemoryRecord.expires_at.is_(None),
            )
        )
        records = list((await session.scalars(stmt)).all())
        for record in records:
            record.expires_at = datetime.now(UTC)
        return len(records)
