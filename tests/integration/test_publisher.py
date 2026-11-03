from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.config import Settings
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import (
    ActionLog,
    Draft,
    MemoryRecord,
    Post,
    PublishingJob,
)
from app.memory.repositories import DraftRepository, PostRepository
from app.publishing.publisher import Publisher
from app.publishing.x_client import (
    AmbiguousPublishError,
    XPost,
    XRateLimitedError,
)

BODY = (
    "The new scheduler batches requests and the paged KV cache cuts fragmentation. "
    "Reported 1,200 tok/s on one CPU core makes local coding agents viable."
)


class FakeXClient:
    configured = True

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, str | None]] = []
        self.counter = 0

    async def create_post(self, text: str, *, in_reply_to: str | None = None, quote: str | None = None) -> XPost:
        if self.error:
            raise self.error
        self.counter += 1
        self.calls.append({"text": text, "in_reply_to": in_reply_to})
        post_id = str(1000 + self.counter)
        return XPost(id=post_id, text=text, url=f"https://x.com/i/web/status/{post_id}", raw={})


def live_settings(settings: Settings, **overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "publishing_enabled": True,
        "dry_run": False,
        "x_publishing_mode": "official_api",
        "x_api_enabled": True,
        "min_hours_between_posts": 0.0,
        "max_original_posts_per_day": 3,
        "x_consumer_key": SecretStr("ck"),
        "x_consumer_secret": SecretStr("cs"),
        "x_access_token": SecretStr("at"),
        "x_access_token_secret": SecretStr("ats"),
    }
    values.update(overrides)
    return settings.model_copy(update=values)


async def create_draft(
    account_id: int,
    *,
    status: str = "ready",
    body: str = BODY,
    thread_parts: list[str] | None = None,
    verification: dict | None = None,
    policy: dict | None = None,
    source_ids: list[int] | None = None,
) -> int:
    async with session_scope() as session:
        draft = await DraftRepository.create(
            session,
            account_id=account_id,
            kind="original",
            status=status,
            idempotency_key=f"test-{body[:10]}-{status}-{len(thread_parts or [])}",
            topic="inference",
            body=body,
            thread_parts=thread_parts or [],
            source_document_ids=source_ids if source_ids is not None else [1],
            verification_report=verification
            if verification is not None
            else {"passed": True, "unverified_facts": 0, "verified_facts": 1},
            policy_report=policy
            if policy is not None
            else {"duplicate": {"duplicate": False}, "trending": False},
        )
        await session.commit()
        return draft.id


def make_publisher(settings: Settings, client: FakeXClient, embedder=None) -> Publisher:  # type: ignore[no-untyped-def]
    return Publisher(
        settings,
        db_module.get_session_factory(),
        client,
        embedder=embedder,
    )


async def test_publishes_once_and_records_everything(settings, engine, account) -> None:
    draft_id = await create_draft(account.id)
    client = FakeXClient()
    publisher = make_publisher(live_settings(settings), client, HashingEmbeddings(384))

    result = await publisher.publish_original(draft_id)
    assert result.status == "published", result.reasons
    assert result.post_ids == ["1001"]
    assert len(client.calls) == 1

    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        post = await session.scalar(select(Post).where(Post.draft_id == draft_id))
        job = await session.scalar(select(PublishingJob).where(PublishingJob.draft_id == draft_id))
        logs = (await session.scalars(select(ActionLog))).all()
        memories = (await session.scalars(select(MemoryRecord))).all()
        published_count = await session.scalar(select(func.count(Post.id)))
    assert draft is not None and draft.status == "published"
    assert post is not None and post.x_post_id == "1001" and post.position == 0
    assert job is not None and job.status == "succeeded"
    assert published_count == 1
    assert any(log.outcome == "published" for log in logs)
    assert any(memory.record_type == "published_post" for memory in memories)

    second = await publisher.publish_original(draft_id)
    assert second.status == "already_published"
    assert len(client.calls) == 1


async def test_dry_run_never_calls_x(settings, engine, account) -> None:
    draft_id = await create_draft(account.id)
    client = FakeXClient()
    dry = live_settings(settings, publishing_enabled=True, dry_run=True)
    result = await make_publisher(dry, client).publish_original(draft_id)
    assert result.status == "simulated"
    assert client.calls == []
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        posts = (await session.scalars(select(Post))).all()
    assert draft is not None and draft.status == "ready"
    assert posts == []


async def test_kill_switch_prevents_call(settings, engine, account) -> None:
    draft_id = await create_draft(account.id)
    client = FakeXClient()
    result = await make_publisher(
        live_settings(settings, publishing_enabled=False), client
    ).publish_original(draft_id)
    assert result.status == "skipped"
    assert client.calls == []


async def test_unverified_draft_is_blocked_and_persisted(settings, engine, account) -> None:
    draft_id = await create_draft(
        account.id, verification={"passed": False, "unverified_facts": 1}
    )
    client = FakeXClient()
    result = await make_publisher(live_settings(settings), client).publish_original(draft_id)
    assert result.status == "blocked"
    assert client.calls == []
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        logs = (await session.scalars(select(ActionLog))).all()
    assert draft is not None and draft.status == "blocked"
    assert any(log.outcome == "blocked" for log in logs)


async def test_duplicate_draft_is_blocked(settings, engine, account) -> None:
    draft_id = await create_draft(
        account.id, policy={"duplicate": {"duplicate": True}, "trending": False}
    )
    result = await make_publisher(live_settings(settings), FakeXClient()).publish_original(draft_id)
    assert result.status == "blocked"
    assert any("duplicate" in reason for reason in result.reasons)


async def test_trending_topic_blocked(settings, engine, account) -> None:
    draft_id = await create_draft(
        account.id, policy={"duplicate": {"duplicate": False}, "trending": True}
    )
    client = FakeXClient()
    result = await make_publisher(live_settings(settings), client).publish_original(draft_id)
    assert result.status == "blocked"
    assert any("trending" in reason for reason in result.reasons)
    assert client.calls == []


async def test_daily_budget_defers_without_blocking(settings, engine, account) -> None:
    draft_id = await create_draft(account.id, body=BODY + " unique budget test")
    async with session_scope() as session:
        for index in range(3):
            await PostRepository.create(
                session,
                account_id=account.id,
                x_post_id=f"budget-{index}",
                kind="original",
                position=0,
                text=f"previous post {index}",
                published_at=datetime.now(UTC) - timedelta(hours=1),
            )
        await session.commit()
    client = FakeXClient()
    result = await make_publisher(live_settings(settings), client).publish_original(draft_id)
    assert result.status == "skipped"
    assert client.calls == []
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
    assert draft is not None and draft.status == "ready"


async def test_rate_limit_leaves_draft_ready_for_retry(settings, engine, account) -> None:
    draft_id = await create_draft(account.id)
    result = await make_publisher(
        live_settings(settings), FakeXClient(XRateLimitedError("slow down", 60))
    ).publish_original(draft_id)
    assert result.status == "failed"
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        job = await session.scalar(select(PublishingJob).where(PublishingJob.draft_id == draft_id))
        audit = (await session.scalars(select(ActionLog))).all()
    assert draft is not None and draft.status == "ready"
    assert job is not None and job.status == "failed"
    assert any(log.outcome == "failed" for log in audit)


async def test_ambiguous_failure_requires_reconciliation(settings, engine, account) -> None:
    draft_id = await create_draft(account.id)
    result = await make_publisher(
        live_settings(settings), FakeXClient(AmbiguousPublishError("timeout after send"))
    ).publish_original(draft_id)
    assert result.status == "unknown"
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
        job = await session.scalar(select(PublishingJob).where(PublishingJob.draft_id == draft_id))
    assert draft is not None and draft.status == "needs_reconciliation"
    assert job is not None and job.status == "unknown"
    retry = await make_publisher(live_settings(settings), FakeXClient()).publish_original(draft_id)
    assert retry.status == "unknown"  # never silently retried


async def test_thread_is_published_as_self_reply_chain(settings, engine, account) -> None:
    draft_id = await create_draft(
        account.id,
        body=BODY,
        thread_parts=["Second part: the trade-off is cache memory, not throughput."],
    )
    client = FakeXClient()
    result = await make_publisher(live_settings(settings), client).publish_original(draft_id)
    assert result.status == "published"
    assert len(result.post_ids) == 2
    assert client.calls[0]["in_reply_to"] is None
    assert client.calls[1]["in_reply_to"] == "1001"
    async with session_scope() as session:
        posts = (
            await session.scalars(select(Post).where(Post.draft_id == draft_id).order_by(Post.position))
        ).all()
    assert [post.position for post in posts] == [0, 1]


async def test_missing_duplicate_report_blocks(settings, engine, account) -> None:
    draft_id = await create_draft(account.id, policy={"trending": False})
    result = await make_publisher(live_settings(settings), FakeXClient()).publish_original(draft_id)
    assert result.status == "blocked"
    assert any("duplicate-check" in reason for reason in result.reasons)


@pytest.mark.parametrize("draft_status", ["blocked", "needs_review", "published"])
async def test_only_ready_drafts_publish(settings, engine, account, draft_status: str) -> None:
    draft_id = await create_draft(account.id, status=draft_status)
    client = FakeXClient()
    result = await make_publisher(live_settings(settings), client).publish_original(draft_id)
    assert result.status in {"blocked", "already_published"}
    assert client.calls == []
