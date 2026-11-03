from __future__ import annotations

from sqlalchemy import select

from app.config import Settings
from app.ingestion.x_research import XMentionsIngestor, is_opt_out
from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.embeddings import HashingEmbeddings
from app.memory.models import ConversationThread, Interaction, MemoryRecord, OptOutRecord
from app.publishing.x_client import XMention


class FakeXClient:
    configured = True

    def __init__(self, mentions: list[XMention]) -> None:
        self.mentions = mentions
        self.me_calls = 0

    async def get_me(self) -> dict[str, str]:
        self.me_calls += 1
        return {"id": "1001", "username": "techbrief", "name": "Tech Brief"}

    async def get_mentions(self, user_id: str, *, max_results: int = 10) -> list[XMention]:
        return self.mentions


def enabled_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"x_research_enabled": True, "x_reads_enabled": True}
    )


def make_mentions() -> list[XMention]:
    return [
        XMention(
            id="9001",
            text="Does the new scheduler help p99 latency?",
            author_id="42",
            author_handle="devperson",
            conversation_id="8000",
            in_reply_to_post_id="7000",
            created_at=None,
        ),
        XMention(
            id="9002",
            text="Please stop replying to me.",
            author_id="43",
            author_handle="tireddev",
            conversation_id="8001",
            in_reply_to_post_id="7000",
            created_at=None,
        ),
    ]


async def test_mention_sync_records_conversation_and_opt_out(settings, engine, account) -> None:
    ingestor = XMentionsIngestor(
        enabled_settings(settings),
        db_module.get_session_factory(),
        FakeXClient(make_mentions()),
        embedder=HashingEmbeddings(384),
    )
    stored = await ingestor.sync(account.id)
    assert stored == 2

    async with session_scope() as session:
        interactions = (await session.scalars(select(Interaction))).all()
        threads = (await session.scalars(select(ConversationThread))).all()
        opt_outs = (await session.scalars(select(OptOutRecord))).all()
        memories = (await session.scalars(select(MemoryRecord))).all()
    assert {i.x_post_id for i in interactions} == {"9001", "9002"}
    assert len(threads) == 2
    assert [record.x_user_id for record in opt_outs] == ["43"]
    assert all(memory.category == "conversation" for memory in memories)
    assert len(memories) == 2


async def test_second_sync_is_idempotent(settings, engine, account) -> None:
    client = FakeXClient(make_mentions())
    ingestor = XMentionsIngestor(
        enabled_settings(settings), db_module.get_session_factory(), client,
        embedder=HashingEmbeddings(384),
    )
    await ingestor.sync(account.id)
    second = await ingestor.sync(account.id)
    assert second == 0
    async with session_scope() as session:
        interactions = (await session.scalars(select(Interaction))).all()
    assert len(interactions) == 2


async def test_sync_is_skipped_when_not_enabled(settings, engine, account) -> None:
    client = FakeXClient(make_mentions())
    ingestor = XMentionsIngestor(settings, db_module.get_session_factory(), client)
    assert await ingestor.sync(account.id) == 0
    assert client.me_calls == 0


def test_opt_out_phrases() -> None:
    assert is_opt_out("please STOP REPLYING to me")
    assert is_opt_out("do not reply, thanks")
    assert is_opt_out("opt out of this")
    assert not is_opt_out("what about p99 latency?")
