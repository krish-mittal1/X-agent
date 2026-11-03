"""Optional X conversation research through authorized official endpoints only.

This adapter is inactive unless both X_RESEARCH_ENABLED and X_READS_ENABLED are
true and OAuth credentials are configured. It reads mentions of this account (not
trends, not keyword searches) and records them as conversation state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import (
    Account,
    ConversationThread,
    Interaction,
    OptOutRecord,
)
from app.memory.vector_store import MemoryVectorStore
from app.publishing.x_client import XMention

logger = get_logger(__name__)

OPT_OUT_PHRASES = (
    "stop replying",
    "do not reply",
    "don't reply",
    "dont reply",
    "opt out",
    "unsubscribe",
    "stop responding",
    "no replies please",
)


def is_opt_out(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in OPT_OUT_PHRASES)


class XMentionsIngestor:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        x_client,  # XClient
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.x_client = x_client
        self.embedder = embedder
        self.store = MemoryVectorStore(settings)

    @property
    def enabled(self) -> bool:
        return (
            self.settings.x_research_enabled
            and self.settings.x_reads_enabled
            and bool(getattr(self.x_client, "configured", False))
        )

    async def sync(self, account_id: int) -> int:
        if not self.enabled:
            logger.info("X mention ingestion skipped (disabled or not authorized)")
            return 0
        async with self.session_factory() as session:
            account = await session.get(Account, account_id)
            if account is None:
                raise ValueError(f"account {account_id} not found")
            user_id = account.x_user_id
            if not user_id:
                me = await self.x_client.get_me()
                user_id = me["id"]
                account.x_user_id = user_id
                account.handle = account.handle or me.get("username", "")
                await session.commit()
            mentions = await self.x_client.get_mentions(user_id)
            stored = 0
            for mention in mentions:
                if mention.author_id == user_id:
                    continue  # never treat our own posts as inbound intent
                if await self._store_mention(session, account_id, mention):
                    stored += 1
            await session.commit()
        logger.info("X mentions synced", extra={"extra_fields": {"stored": stored}})
        return stored

    async def _fetch_thread_ancestors(
        self,
        session: AsyncSession,
        account_id: int,
        thread: ConversationThread,
        parent_id: str | None,
        depth: int = 3,
    ) -> None:
        """Fetch parent and ancestor tweets in a thread to build complete context."""
        current_id = parent_id
        while current_id and depth > 0:
            depth -= 1
            existing = await session.scalar(
                select(Interaction).where(
                    Interaction.account_id == account_id,
                    Interaction.x_post_id == current_id,
                )
            )
            if existing is not None:
                break
            if not hasattr(self.x_client, "get_tweets"):
                break
            try:
                ancestors = await self.x_client.get_tweets([current_id])
            except Exception as exc:
                logger.warning(
                    "failed to fetch thread ancestor",
                    extra={"extra_fields": {"tweet_id": current_id, "error": str(exc)[:200]}},
                )
                break
            if not ancestors:
                break
            ancestor = ancestors[0]
            session.add(
                Interaction(
                    account_id=account_id,
                    conversation_thread_id=thread.id,
                    x_post_id=ancestor.id,
                    author_x_user_id=ancestor.author_id,
                    author_handle=ancestor.author_handle,
                    direction="inbound",
                    text=ancestor.text,
                    occurred_at=datetime.now(UTC),
                    metadata_={
                        "conversation_id": thread.x_conversation_id,
                        "is_ancestor": True,
                    },
                )
            )
            current_id = ancestor.in_reply_to_post_id

    async def _store_mention(
        self, session: AsyncSession, account_id: int, mention: XMention
    ) -> bool:
        existing = await session.scalar(
            select(Interaction).where(
                Interaction.account_id == account_id, Interaction.x_post_id == mention.id
            )
        )
        if existing is not None:
            return False
        conversation_id = mention.conversation_id or mention.id
        thread = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.account_id == account_id,
                ConversationThread.x_conversation_id == conversation_id,
            )
        )
        if thread is None:
            thread = ConversationThread(
                account_id=account_id,
                x_conversation_id=conversation_id,
                root_post_id=mention.in_reply_to_post_id or mention.id,
                topic=mention.text[:200],
                last_activity_at=datetime.now(UTC),
            )
            session.add(thread)
            await session.flush()
        thread.last_activity_at = datetime.now(UTC)

        if mention.in_reply_to_post_id:
            await self._fetch_thread_ancestors(
                session, account_id, thread, mention.in_reply_to_post_id
            )

        session.add(
            Interaction(
                account_id=account_id,
                conversation_thread_id=thread.id,
                x_post_id=mention.id,
                author_x_user_id=mention.author_id,
                author_handle=mention.author_handle,
                direction="inbound",
                text=mention.text,
                occurred_at=datetime.now(UTC),
                metadata_={"conversation_id": conversation_id},
            )
        )
        if is_opt_out(mention.text) and mention.author_id:
            record = await session.scalar(
                select(OptOutRecord).where(
                    OptOutRecord.account_id == account_id,
                    OptOutRecord.x_user_id == mention.author_id,
                )
            )
            if record is None:
                session.add(
                    OptOutRecord(
                        account_id=account_id,
                        x_user_id=mention.author_id,
                        handle=mention.author_handle,
                        reason="opt-out phrase in mention",
                        source_post_id=mention.id,
                    )
                )
                thread.state = {**(thread.state or {}), "opted_out": True}
        if self.embedder is not None:
            await self.store.add_memory(
                session,
                account_id=account_id,
                category="conversation",
                record_type="inbound_message",
                content=f"@{mention.author_handle or mention.author_id}: {mention.text}",
                conversation_thread_id=thread.id,
                confidence=1.0,
                provenance={
                    "x_post_id": mention.id,
                    "author_x_user_id": mention.author_id,
                    "author_handle": mention.author_handle,
                    "source": "x_mentions",
                },
                embedding_provider=self.embedder,
            )
        return True
