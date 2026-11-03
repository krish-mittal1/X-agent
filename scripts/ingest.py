"""Run one research ingestion pass.

Usage: python -m scripts.ingest [--source rss|github|all] [--score/--no-score]
"""

from __future__ import annotations

import argparse
import asyncio

from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.embeddings import build_embedding_provider
from app.memory.repositories import AccountRepository
from app.research.discovery import DiscoveryService
from scripts._common import prepare


async def main(source: str, score: bool) -> None:
    settings = prepare()
    init_engine(settings)
    try:
        try:
            embedder = build_embedding_provider(settings)
        except Exception as exc:
            print(f"warning: embedding provider unavailable ({exc}); storing text only")
            embedder = None
        async with get_session_factory()() as session:
            account = await AccountRepository.get_default(session, settings.x_persona_handle)
        if account is None:
            raise SystemExit("no account: run python -m scripts.bootstrap first")
        service = DiscoveryService(settings, get_session_factory(), embedder=embedder)
        filters = {"rss": {"rss"}, "github": {"github"}, "all": None}[source]
        stats = await service.run(account.id, source_filter=filters, score=score)
        print(stats.model_dump_json(indent=2))
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["rss", "github", "all"], default="all")
    parser.add_argument("--score", dest="score", action="store_true", default=True)
    parser.add_argument("--no-score", dest="score", action="store_false")
    args = parser.parse_args()
    asyncio.run(main(args.source, args.score))
