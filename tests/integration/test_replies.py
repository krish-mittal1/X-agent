from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.agent.reply_drafter import ReplyDrafter
from app.config import Settings
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import (
    ActionLog,
    ConversationThread,
    Draft,
    Interaction,
    OptOutRecord,
    Post,
)
from app.providers.base import LLMResponse, LLMUsage, ProviderUnavailableError
from app.publishing.publisher import Publisher
from app.publishing.reply_queue import ReplyQueue
from app.publishing.x_client import AmbiguousPublishError, XPost
from app.schemas.content import ContentGenerationError, DraftClaim, ReplyDraftContent

REPLY_BODY = "The batching scheduler matters more than the cache change here; the trade-off is tail latency."


class FakeLLM:
    def __init__(self, content: ReplyDraftContent | Exception) -> None:
        self.content = content
        self.calls = 0

    async def generate_structured(self, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        return self.content, LLMResponse(
            text="{}", model="fake", usage=LLMUsage(), latency_ms=1.0, estimated_cost_usd=0.0
        )


class FakeXClient:
    configured = True

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def create_post(self, text: str, *, in_reply_to: str | None = None, quote: str | None = None) -> XPost:
        if self.error:
            raise self.error
        self.calls.append((text, in_reply_to))
        return XPost(id="777", text=text, url="https://x.com/i/web/status/777", raw={})


def reply_content(send: bool = True, claims: list[DraftClaim] | None = None) -> ReplyDraftContent:
    return ReplyDraftContent(
        parts=[REPLY_BODY],
        rationale="Adds the specific trade-off to the conversation.",
        claims=claims or [],
        send_recommended=send,
        uncertainty=["Tail-latency impact depends on batch size."],
    )


async def seed_conversation(
    account_id: int,
    *,
    opt_out: bool = False,
    x_post_id: str = "555",
    author: str = "42",
) -> tuple[int, str]:
    async with session_scope() as session:
        thread = ConversationThread(
            account_id=account_id, x_conversation_id="conv-1", topic="scheduler"
        )
        session.add(thread)
        await session.flush()
        session.add(
            Interaction(
                account_id=account_id,
                conversation_thread_id=thread.id,
                x_post_id=x_post_id,
                author_x_user_id=author,
                author_handle="devperson",
                direction="inbound",
                text="Does the new scheduler actually improve p99 latency?",
                occurred_at=datetime.now(UTC),
            )
        )
        if opt_out:
            session.add(
                OptOutRecord(
                    account_id=account_id,
                    x_user_id=author,
                    handle="devperson",
                    reason="asked to opt out",
                )
            )
        await session.commit()
        return thread.id, x_post_id


def make_drafter(settings: Settings, llm: FakeLLM) -> ReplyDrafter:
    return ReplyDrafter(
        settings, llm, db_module.get_session_factory(), embedder=HashingEmbeddings(384)
    )


async def test_reply_draft_is_created_for_review_only(settings, engine, account) -> None:
    await seed_conversation(account.id)
    llm = FakeLLM(reply_content())
    draft = await make_drafter(settings, llm).draft_reply(account.id, "555")
    assert draft is not None
    assert draft.status == "needs_review"
    assert draft.kind == "reply"
    assert draft.in_reply_to_post_id == "555"
    assert draft.target_author_handle == "devperson"
    assert draft.body == REPLY_BODY
    async with session_scope() as session:
        posts = (await session.scalars(select(Post))).all()
    assert posts == []  # nothing is sent while drafting

    again = await make_drafter(settings, llm).draft_reply(account.id, "555")
    assert again is not None and again.id == draft.id
    assert llm.calls == 1


async def test_reply_drafting_requires_inbound_interaction(settings, engine, account) -> None:
    with pytest.raises(ValueError, match="inbound interaction"):
        await make_drafter(settings, FakeLLM(reply_content())).draft_reply(account.id, "999")


async def test_opt_out_prevents_drafting(settings, engine, account) -> None:
    await seed_conversation(account.id, opt_out=True)
    draft = await make_drafter(settings, FakeLLM(reply_content())).draft_reply(account.id, "555")
    assert draft is None
    async with session_scope() as session:
        assert (await session.scalars(select(Draft))).all() == []


async def test_unsupported_factual_claim_blocks_reply(settings, engine, account) -> None:
    await seed_conversation(account.id)
    claim = DraftClaim(
        claim="p99 latency improved 40%.",
        claim_type="fact",
        supporting_quote="p99 latency improved 40%",
        source_url="https://example.com/source",
    )
    draft = await make_drafter(settings, FakeLLM(reply_content(claims=[claim]))).draft_reply(
        account.id, "555"
    )
    assert draft is not None
    assert draft.status == "blocked"
    assert draft.error and "unverified factual claim" in draft.error


async def test_no_reply_recommendation_creates_no_draft(settings, engine, account) -> None:
    await seed_conversation(account.id)
    draft = await make_drafter(settings, FakeLLM(reply_content(send=False))).draft_reply(
        account.id, "555"
    )
    assert draft is None
    async with session_scope() as session:
        interactions = (await session.scalars(select(Interaction))).all()
    assert any(i.metadata_.get("no_reply_recommended") for i in interactions)


async def test_provider_failure_does_not_create_reply_draft(settings, engine, account) -> None:
    await seed_conversation(account.id)
    with pytest.raises(ContentGenerationError):
        await make_drafter(settings, FakeLLM(ProviderUnavailableError("down"))).draft_reply(
            account.id, "555"
        )


async def test_review_queue_approves_but_does_not_publish(settings, engine, account) -> None:
    await seed_conversation(account.id)
    draft = await make_drafter(settings, FakeLLM(reply_content())).draft_reply(account.id, "555")
    assert draft is not None
    async with session_scope() as session:
        approved = await ReplyQueue.approve(
            session, account.id, draft.id, reviewer="alice", notes="looks good"
        )
        await session.commit()
    assert approved.status == "approved"
    assert approved.reviewed_by == "alice"
    async with session_scope() as session:
        assert (await session.scalars(select(Post))).all() == []
        logs = (await session.scalars(select(ActionLog))).all()
    assert any(log.action == "reply_draft_review" and log.outcome == "approved" for log in logs)


async def test_invalid_review_transitions(settings, engine, account) -> None:
    await seed_conversation(account.id)
    draft = await make_drafter(settings, FakeLLM(reply_content())).draft_reply(account.id, "555")
    assert draft is not None
    async with session_scope() as session:
        with pytest.raises(ValueError, match="reason"):
            await ReplyQueue.reject(session, account.id, draft.id, reviewer="a", notes="")
        await ReplyQueue.approve(session, account.id, draft.id, reviewer="a")
        await session.commit()
    async with session_scope() as session:
        with pytest.raises(ValueError, match="not awaiting review"):
            await ReplyQueue.approve(session, account.id, draft.id, reviewer="b")


def reply_live_settings(settings: Settings, **overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "ai_reply_approval_obtained": True,
        "ai_reply_publishing_enabled": True,
        "dry_run": False,
        "x_publishing_mode": "official_api",
        "x_api_enabled": True,
        "x_consumer_key": SecretStr("ck"),
        "x_consumer_secret": SecretStr("cs"),
        "x_access_token": SecretStr("at"),
        "x_access_token_secret": SecretStr("ats"),
        "max_replies_per_day": 5,
    }
    values.update(overrides)
    return Settings(**{**settings.model_dump(), **values})


async def approved_reply_draft(
    settings: Settings, account_id: int, llm: FakeLLM | None = None
) -> int:
    await seed_conversation(account_id)
    draft = await make_drafter(settings, llm or FakeLLM(reply_content())).draft_reply(
        account_id, "555"
    )
    assert draft is not None
    async with session_scope() as session:
        await ReplyQueue.approve(session, account_id, draft.id, reviewer="alice")
        await session.commit()
    return draft.id


async def test_reply_sending_disabled_by_default(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    client = FakeXClient()
    result = await Publisher(settings, db_module.get_session_factory(), client).publish_reply(draft_id)
    assert result.status == "skipped"
    assert client.calls == []
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
    assert draft is not None and draft.status == "approved"  # preserved for later review


async def test_reply_dry_run_never_calls_x(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    client = FakeXClient()
    dry = reply_live_settings(settings, dry_run=True)
    result = await Publisher(dry, db_module.get_session_factory(), client).publish_reply(draft_id)
    assert result.status == "simulated"
    assert client.calls == []
    async with session_scope() as session:
        assert (await session.scalars(select(Post))).all() == []


async def test_reply_publishes_only_with_all_gates(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    client = FakeXClient()
    live = reply_live_settings(settings)
    result = await Publisher(live, db_module.get_session_factory(), client).publish_reply(draft_id)
    assert result.status == "published", result.reasons
    assert client.calls == [(REPLY_BODY, "555")]
    async with session_scope() as session:
        posts = (await session.scalars(select(Post))).all()
        draft = await session.get(Draft, draft_id)
    assert len(posts) == 1 and posts[0].kind == "reply"
    assert posts[0].in_reply_to_post_id == "555"
    assert draft is not None and draft.status == "published"


async def test_unapproved_reply_never_publishes(settings, engine, account) -> None:
    await seed_conversation(account.id)
    draft = await make_drafter(settings, FakeLLM(reply_content())).draft_reply(account.id, "555")
    assert draft is not None
    client = FakeXClient()
    result = await Publisher(
        reply_live_settings(settings), db_module.get_session_factory(), client
    ).publish_reply(draft.id)
    assert result.status == "blocked"
    assert client.calls == []


async def test_opt_out_at_publish_time_blocks(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    async with session_scope() as session:
        session.add(
            OptOutRecord(account_id=account.id, x_user_id="42", handle="devperson",
                         reason="late opt out")
        )
        await session.commit()
    client = FakeXClient()
    result = await Publisher(
        reply_live_settings(settings), db_module.get_session_factory(), client
    ).publish_reply(draft_id)
    assert result.status == "blocked"
    assert client.calls == []


async def test_ambiguous_reply_requires_reconciliation(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    client = FakeXClient(AmbiguousPublishError("timeout"))
    result = await Publisher(
        reply_live_settings(settings), db_module.get_session_factory(), client
    ).publish_reply(draft_id)
    assert result.status == "unknown"
    async with session_scope() as session:
        draft = await session.get(Draft, draft_id)
    assert draft is not None and draft.status == "needs_reconciliation"


async def test_reply_budget_blocks_further_sends(settings, engine, account) -> None:
    draft_id = await approved_reply_draft(settings, account.id)
    async with session_scope() as session:
        await session.flush()
        session.add(
            Post(
                account_id=account.id,
                x_post_id="already-1",
                kind="reply",
                position=0,
                text="previous reply",
                published_at=datetime.now(UTC),
            )
        )
        await session.commit()
    live = reply_live_settings(settings, max_replies_per_day=1)
    client = FakeXClient()
    result = await Publisher(live, db_module.get_session_factory(), client).publish_reply(draft_id)
    assert result.status == "skipped"
    assert any("replies in the last 24h" in reason for reason in result.reasons)
