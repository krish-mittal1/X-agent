from __future__ import annotations

from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import SourceDocument
from app.memory.vector_store import MemoryVectorStore


class RecordingEmbeddings(HashingEmbeddings):
    def __init__(self) -> None:
        super().__init__(384)
        self.batch_sizes: list[int] = []

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        self.batch_sizes.append(len(texts))
        return await super().embed(texts)


async def test_source_chunks_are_embedded_in_batches(settings, engine, account) -> None:
    provider = RecordingEmbeddings()
    settings = settings.model_copy(
        update={
            "embedding_batch_size": 4,
            "embedding_chunk_words": 20,
            "embedding_chunk_overlap": 5,
            "embedding_min_chunk_words": 5,
        }
    )
    store = MemoryVectorStore(settings)
    document = SourceDocument(
        account_id=account.id,
        source_type="rss",
        source_identifier="feed",
        url="https://example.com/long",
        url_hash="long-hash",
        canonical_url="https://example.com/long",
        title="Long technical note",
        content=" ".join(f"word{i}" for i in range(120)),
        content_hash="long-content",
        excerpt="long",
        reliability_score=0.7,
        fetch_status="ok",
    )
    async with session_scope() as session:
        session.add(document)
        await session.flush()
        result = await store.index_source_document(session, document, provider)
        await session.commit()
    assert result.chunks > 4
    assert result.embedded == result.chunks
    assert provider.batch_sizes[0] == 4
    assert all(size <= 4 for size in provider.batch_sizes)
    assert sum(provider.batch_sizes) == result.chunks
