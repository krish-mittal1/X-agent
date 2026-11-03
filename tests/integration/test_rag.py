from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import (
    MEMORY_CONVERSATION,
    MEMORY_EPISODIC,
    MEMORY_SEMANTIC,
    ConversationThread,
    MemoryEmbedding,
    MemoryRecord,
    SourceDocument,
)
from app.memory.repositories import SourceRepository
from app.memory.retrieval import HybridRetriever
from app.memory.vector_store import MemoryVectorStore
from app.schemas.research import IngestedItem


def make_embedder() -> HashingEmbeddings:
    return HashingEmbeddings(384)


async def store_and_index(
    settings: Settings,
    account_id: int,
    embedder: HashingEmbeddings,
    *,
    url: str,
    title: str,
    content: str,
    source_type: str = "rss",
) -> SourceDocument:
    async with session_scope() as session:
        document, created = await SourceRepository.store_item(
            session,
            account_id,
            IngestedItem(
                source_type=source_type,  # type: ignore[arg-type]
                source_identifier="feed",
                url=url,
                title=title,
                content=content,
                excerpt=content[:300],
                published_at=datetime.now(UTC),
                reliability_score=0.8,
                metadata={"is_primary_source": True},
            ),
        )
        assert document is not None and created
        store = MemoryVectorStore(settings)
        await store.index_source_document(session, document, embedder)
        await session.commit()
        return document


async def add_conversation_memory(
    settings: Settings,
    embedder: HashingEmbeddings,
    *,
    account_id: int,
    thread_id: int,
    content: str,
) -> MemoryRecord:
    async with session_scope() as session:
        store = MemoryVectorStore(settings)
        record, embedded = await store.add_memory(
            session,
            account_id=account_id,
            category=MEMORY_CONVERSATION,
            record_type="thread_message",
            content=content,
            conversation_thread_id=thread_id,
            confidence=0.7,
            provenance={"source": "conversation"},
            embedding_provider=embedder,
        )
        assert embedded == 1
        await session.commit()
        return record


async def test_source_indexing_persists_embeddings(settings, engine, account) -> None:
    embedder = make_embedder()
    document = await store_and_index(
        settings,
        account.id,
        embedder,
        url="https://example.com/pgvector",
        title="pgvector 0.8 adds iterative index scans",
        content="pgvector supports HNSW and IVFFlat indexes for cosine similarity in PostgreSQL.",
    )
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(MemoryEmbedding.id)).where(
                MemoryEmbedding.embedding_model == embedder.model_name
            )
        )
        dims = await session.scalar(select(MemoryEmbedding.dimensions).limit(1))
        record = await session.scalar(
            select(MemoryRecord).where(MemoryRecord.source_document_id == document.id)
        )
    assert count and count >= 1
    assert dims == 384
    assert record is not None
    assert record.category == MEMORY_SEMANTIC
    assert record.provenance["source_url"] == "https://example.com/pgvector"
    assert record.provenance["reliability"] == pytest.approx(0.8)


async def test_hybrid_retrieval_returns_relevant_memory(settings, engine, account) -> None:
    embedder = make_embedder()
    await store_and_index(
        settings,
        account.id,
        embedder,
        url="https://example.com/pgvector",
        title="pgvector hybrid retrieval benchmarks",
        content="Postgres pgvector HNSW index gives fast cosine similarity search for embeddings.",
    )
    await store_and_index(
        settings,
        account.id,
        embedder,
        url="https://example.com/css",
        title="CSS container queries",
        content="Container queries let components respond to their own width instead of viewport.",
    )
    retriever = HybridRetriever(settings, embedder)
    async with session_scope() as session:
        result = await retriever.retrieve(
            session, account_id=account.id, query="pgvector cosine similarity embeddings"
        )
    assert result.used_vector is True
    assert result.used_fts is True
    assert result.memories
    top = result.memories[0]
    assert "pgvector" in top.record.content.lower()
    assert top.signals["vector_rank"] is not None
    assert top.score > 0
    assert "sim=" in top.explanation
    assert any(item["source_url"] for item in result.as_prompt_context())


async def test_memory_isolation_between_accounts(settings, engine, account_factory) -> None:
    embedder = make_embedder()
    account_a = await account_factory("alpha")
    account_b = await account_factory("beta", "2002")
    await store_and_index(
        settings, account_a.id, embedder,
        url="https://example.com/a", title="Alpha secret deployment plan",
        content="Alpha uses blue-green deployment with a canary stage.",
    )
    await store_and_index(
        settings, account_b.id, embedder,
        url="https://example.com/b", title="Beta secret deployment plan",
        content="Beta uses rolling deployment with feature flags.",
    )
    retriever = HybridRetriever(settings, embedder)
    async with session_scope() as session:
        result_a = await retriever.retrieve(
            session, account_id=account_a.id, query="deployment plan blue canary flags"
        )
        result_b = await retriever.retrieve(
            session, account_id=account_b.id, query="deployment plan blue canary flags"
        )
    assert result_a.memories and result_b.memories
    assert all(m.record.account_id == account_a.id for m in result_a.memories)
    assert all(m.record.account_id == account_b.id for m in result_b.memories)
    assert all("Alpha" in m.record.content for m in result_a.memories)


async def test_conversation_isolation_and_thread_context(
    settings, engine, account
) -> None:
    embedder = make_embedder()
    async with session_scope() as session:
        thread_one = ConversationThread(
            account_id=account.id, x_conversation_id="100", topic="inference latency"
        )
        thread_two = ConversationThread(
            account_id=account.id, x_conversation_id="200", topic="pricing"
        )
        session.add_all([thread_one, thread_two])
        await session.flush()
        await session.commit()
        one_id, two_id = thread_one.id, thread_two.id

    await add_conversation_memory(
        settings, embedder, account_id=account.id, thread_id=one_id,
        content="We told this person the inference latency fix ships in v2.1 next week.",
    )
    await add_conversation_memory(
        settings, embedder, account_id=account.id, thread_id=two_id,
        content="We told another person pricing details are not public yet.",
    )
    async with session_scope() as session:
        store = MemoryVectorStore(settings)
        await store.add_memory(
            session,
            account_id=account.id,
            category=MEMORY_SEMANTIC,
            record_type="fact",
            content="Inference latency is dominated by KV cache memory bandwidth.",
            provenance={"reliability": 0.9},
            embedding_provider=embedder,
        )
        await session.commit()

    retriever = HybridRetriever(settings, embedder)
    async with session_scope() as session:
        result = await retriever.retrieve(
            session,
            account_id=account.id,
            query="inference latency pricing",
            conversation_thread_id=one_id,
        )
    thread_ids = {m.record.conversation_thread_id for m in result.memories}
    assert one_id in thread_ids
    assert two_id not in thread_ids
    contents = " ".join(m.record.content for m in result.memories)
    assert "pricing details" not in contents

    async with session_scope() as session:
        account_wide = await retriever.retrieve(
            session, account_id=account.id, query="inference latency pricing"
        )
    assert all(m.record.conversation_thread_id is None for m in account_wide.memories)


async def test_retrieval_degrades_to_full_text_without_embedder(settings, engine, account) -> None:
    embedder = make_embedder()
    await store_and_index(
        settings, account.id, embedder,
        url="https://example.com/kv", title="KV cache paging",
        content="Paged KV cache reduces fragmentation in long-context inference.",
    )
    retriever = HybridRetriever(settings, None)
    async with session_scope() as session:
        result = await retriever.retrieve(session, account_id=account.id, query="paged KV cache")
    assert result.used_vector is False
    assert result.used_fts is True
    assert result.degraded_reason is not None
    assert result.memories
    assert all(m.signals["vector_rank"] is None for m in result.memories)


async def test_novelty_score_uses_published_post_embeddings(settings, engine, account) -> None:
    embedder = make_embedder()
    async with session_scope() as session:
        store = MemoryVectorStore(settings)
        await store.add_memory(
            session,
            account_id=account.id,
            category=MEMORY_EPISODIC,
            record_type="published_post",
            content="New inference engine cuts p95 latency with a paged KV cache.",
            provenance={"reliability": 1.0},
            embedding_provider=embedder,
        )
        await session.commit()

    retriever = HybridRetriever(settings, embedder)
    async with session_scope() as session:
        near_duplicate = await retriever.novelty_score(
            session, account_id=account.id,
            text="New inference engine cuts p95 latency with a paged KV cache.",
        )
        unrelated = await retriever.novelty_score(
            session, account_id=account.id,
            text="CSS container queries for responsive component layouts",
        )
    assert near_duplicate is not None and near_duplicate < 0.2
    assert unrelated is not None and unrelated > 0.7


async def test_consolidation_expires_conversations_and_invalidates_failed_sources(
    settings, engine, account
) -> None:
    from app.memory.consolidation import MemoryConsolidator

    async with session_scope() as session:
        thread = ConversationThread(account_id=account.id, x_conversation_id="900")
        session.add(thread)
        await session.flush()
        thread_id = thread.id
        doc = SourceDocument(
            account_id=account.id,
            source_type="rss",
            source_identifier="feed",
            url="https://example.com/failed",
            url_hash="failed-hash",
            canonical_url="https://example.com/failed",
            title="Failed fetch",
            content="partial",
            content_hash="failed-content",
            fetch_status="failed",
            reliability_score=0.6,
            excerpt="partial",
        )
        session.add(doc)
        await session.flush()
        record = MemoryRecord(
            account_id=account.id,
            category=MEMORY_SEMANTIC,
            record_type="source_chunk",
            content="claim from a source that failed to load",
            source_document_id=doc.id,
            content_hash="m1",
            provenance={"reliability": 0.6},
        )
        old_conversation = MemoryRecord(
            account_id=account.id,
            category=MEMORY_CONVERSATION,
            record_type="thread_message",
            content="old chat",
            conversation_thread_id=thread_id,
            content_hash="m2",
            created_at=datetime.now(UTC) - timedelta(days=90),
        )
        session.add_all([record, old_conversation])
        await session.commit()
        record_id, conversation_id = record.id, old_conversation.id

    async with session_scope() as session:
        report = await MemoryConsolidator(settings).run(session, account.id)
        await session.commit()
    async with session_scope() as session:
        semantic = await session.get(MemoryRecord, record_id)
        conversation = await session.get(MemoryRecord, conversation_id)
    assert report["promoted_to_verified_fact"] is False
    assert report["invalidated_semantic_memories"] == 1
    assert report["expired_conversation_memories"] == 1
    assert semantic is not None and semantic.invalidated_at is not None
    assert conversation is not None and conversation.expires_at is not None
