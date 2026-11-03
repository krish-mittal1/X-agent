"""Re-embed indexed content after changing EMBEDDING_MODEL.

Embeddings from different models are never mixed; retrieval only considers the
currently configured model, so old vectors become dormant. This script adds
vectors for the new model to existing memory records.

If the new model's dimension differs from the database column, a migration and a
fresh schema are required first (memory_embeddings.embedding is vector(384)).

Usage: EMBEDDING_MODEL=... python -m scripts.reembed
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.embeddings import build_embedding_provider
from app.memory.models import Post, SourceDocument
from app.memory.vector_store import MemoryVectorStore
from scripts._common import prepare


async def main() -> None:
    settings = prepare()
    init_engine(settings)
    store = MemoryVectorStore(settings)
    try:
        embedder = build_embedding_provider(settings)
        print(f"embedding model: {getattr(embedder, 'model_name', '?')}")
        sources = posts = chunks = 0
        async with get_session_factory()() as session:
            documents = list((await session.scalars(select(SourceDocument))).all())
            for document in documents:
                result = await store.index_source_document(session, document, embedder)
                chunks += result.chunks
                sources += 1
                await session.commit()
            published = list((await session.scalars(select(Post))).all())
            for post in published:
                await store.add_memory(
                    session,
                    account_id=post.account_id,
                    category="episodic",
                    record_type="published_post",
                    content=post.text,
                    summary=f"Published post {post.x_post_id}",
                    post_id=post.id,
                    confidence=1.0,
                    provenance={"x_post_id": post.x_post_id, "url": post.url},
                    embedding_provider=embedder,
                )
                posts += 1
                await session.commit()
        print(f"re-embedded {sources} source documents ({chunks} chunks) and {posts} posts")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
