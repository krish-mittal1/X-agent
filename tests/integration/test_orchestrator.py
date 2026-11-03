from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.agent.orchestrator import AgentOrchestrator
from app.config import Settings
from app.ingestion.base import AdapterResult
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import Draft, Post, PublishingJob
from app.providers.base import LLMResponse, LLMUsage
from app.publishing.x_client import XPost
from app.research.discovery import DiscoveryService
from app.schemas.content import DraftClaim, DraftContent
from app.schemas.research import IngestedItem

SOURCE_TEXT = "Benchmarks report 1,200 tokens per second on a single CPU core with the new scheduler."
BODY = (
    "A paged KV cache plus batching is the practical combination for local coding agents: "
    "the reported 1,200 tok/s on one CPU core is the number to watch. "
    "https://github.com/acme/widget/releases/tag/v2"
)


class FakeAdapter:
    name = "fake"

    async def fetch(self) -> AdapterResult:
        return AdapterResult(
            items=[
                IngestedItem(
                    source_type="github",
                    source_identifier="acme/widget",
                    url="https://github.com/acme/widget/releases/tag/v2",
                    title="Widget v2: batching scheduler",
                    content=SOURCE_TEXT,
                    excerpt=SOURCE_TEXT,
                    published_at=datetime.now(UTC),
                    reliability_score=0.85,
                    metadata={"is_primary_source": True},
                )
            ]
        )


class FakeLLMService:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls = 0

    async def generate_structured(self, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        content = DraftContent(
            topic="Batching scheduler",
            parts=[BODY],
            rationale="Concrete throughput result with a deployment trade-off.",
            claims=[
                DraftClaim(
                    claim="The new scheduler reaches 1,200 tokens per second on one CPU core.",
                    claim_type="fact",
                    supporting_quote="1,200 tokens per second on a single CPU core",
                    source_url="https://github.com/acme/widget/releases/tag/v2",
                )
            ],
            uncertainty=["Not independently reproduced."],
        )
        return content, LLMResponse(
            text="{}", model="fake", usage=LLMUsage(), latency_ms=1.0, estimated_cost_usd=0.0
        )


class FakeXClient:
    configured = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_post(self, text: str, *, in_reply_to: str | None = None, quote: str | None = None) -> XPost:
        self.calls.append(text)
        return XPost(id="123", text=text, url="https://x.com/i/web/status/123", raw={})


def controlled_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "x_publishing_mode": "official_api",
            "x_api_enabled": True,
            "discovery_enabled": True,
            "publishing_enabled": True,
            "dry_run": True,
            "min_candidate_score": 0.0,
            "max_posts_per_run": 1,
            "min_hours_between_posts": 0.0,
            "max_original_posts_per_day": 3,
        }
    )


def make_orchestrator(
    settings: Settings, llm: FakeLLMService, embedder: HashingEmbeddings, x_client: FakeXClient
) -> AgentOrchestrator:
    discovery = DiscoveryService(
        settings,
        db_module.get_session_factory(),
        adapters=[FakeAdapter()],
        embedder=embedder,
    )
    return AgentOrchestrator(
        settings,
        db_module.get_session_factory(),
        llm,  # type: ignore[arg-type]
        embedder=embedder,
        x_client=x_client,
        discovery=discovery,
    )


async def test_end_to_end_dry_run_cycle(settings, engine, account) -> None:
    configured = controlled_settings(settings)
    llm = FakeLLMService()
    x_client = FakeXClient()
    orchestrator = make_orchestrator(configured, llm, HashingEmbeddings(384), x_client)

    report = await orchestrator.run_cycle(account.id)
    assert report.errors == []
    assert report.discovered == 1
    assert report.candidates == 1
    assert report.drafts_created == 1
    assert report.simulated == 1
    assert report.published == 0
    assert x_client.calls == []

    async with session_scope() as session:
        draft = await session.scalar(select(Draft))
        posts = (await session.scalars(select(Post))).all()
        job = await session.scalar(select(PublishingJob))
    assert draft is not None and draft.status == "ready"
    assert draft.verification_report["passed"] is True
    assert posts == []
    assert job is not None and job.status == "simulated"


async def test_cycle_skips_drafting_without_llm(settings, engine, account) -> None:
    configured = controlled_settings(settings)
    orchestrator = make_orchestrator(
        configured, FakeLLMService(enabled=False), HashingEmbeddings(384), FakeXClient()
    )
    report = await orchestrator.run_cycle(account.id)
    assert report.drafts_created == 0
    assert report.skipped_reason is not None and "LLM" in report.skipped_reason


async def test_unverified_draft_from_cycle_is_blocked(settings, engine, account) -> None:
    # Pin a reporting format: an undeclared number only blocks in factual shapes.
    configured = controlled_settings(settings).model_copy(
        update={"post_format_mix": "explainer:1"}
    )
    llm = FakeLLMService()

    async def bad_generate(messages, schema, **kwargs):  # type: ignore[no-untyped-def]
        content, response = await FakeLLMService.generate_structured(llm, messages, schema)
        content.claims = []
        return content, response

    llm.generate_structured = bad_generate  # type: ignore[method-assign]
    orchestrator = make_orchestrator(configured, llm, HashingEmbeddings(384), FakeXClient())
    report = await orchestrator.run_cycle(account.id)
    assert report.drafts_blocked == 1
    assert report.simulated == 0
    async with session_scope() as session:
        draft = await session.scalar(select(Draft))
    assert draft is not None and draft.status == "blocked"
