"""Run one full agent cycle (discover -> score -> draft -> gate -> publish).

Dry-run is the default via DRY_RUN=true. Live publishing additionally requires
PUBLISHING_ENABLED=true and this script's explicit acknowledgement flag.

Usage:
  python -m scripts.cycle
  python -m scripts.cycle --i-understand-live-publishing   # only when configured live
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from app.agent.orchestrator import AgentOrchestrator
from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.embeddings import build_embedding_provider
from app.memory.repositories import AccountRepository
from app.providers.factory import LLMService, build_provider
from app.publishing.x_client import XClient
from scripts._common import prepare


async def main(acknowledge_live: bool) -> None:
    settings = prepare()
    if settings.publishing_enabled and not settings.dry_run and not acknowledge_live:
        raise SystemExit(
            "Refusing to run: PUBLISHING_ENABLED=true and DRY_RUN=false. "
            "Re-run with --i-understand-live-publishing to actually post through X."
        )
    init_engine(settings)
    try:
        try:
            embedder = build_embedding_provider(settings)
        except Exception as exc:
            print(f"warning: embedding provider unavailable ({exc}); RAG disabled")
            embedder = None
        provider = build_provider(settings)
        llm = LLMService(settings, provider, get_session_factory())
        x_client = XClient(settings) if settings.x_write_credentials_present else None
        async with get_session_factory()() as session:
            account = await AccountRepository.get_default(session, settings.x_persona_handle)
        if account is None:
            raise SystemExit("no account: run python -m scripts.bootstrap first")
        orchestrator = AgentOrchestrator(
            settings,
            get_session_factory(),
            llm,
            embedder=embedder,
            x_client=x_client,
        )
        try:
            report = await orchestrator.run_cycle(account.id)
            print(json.dumps(asdict(report), indent=2))
        finally:
            await llm.aclose()
            if x_client is not None:
                await x_client.aclose()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--i-understand-live-publishing",
        action="store_true",
        dest="acknowledge_live",
        help="allow a live (non-dry-run) publish when configuration enables it",
    )
    args = parser.parse_args()
    asyncio.run(main(args.acknowledge_live))
