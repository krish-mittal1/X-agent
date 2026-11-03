"""Original-post publishing through the official X API.

Safety properties:
  * every publish attempt is evaluated by the deterministic policy gate;
  * a PostgreSQL advisory lock prevents two workers publishing one draft;
  * drafts are published at most once (unique job key + per-position post rows);
  * ambiguous network failures are never retried automatically;
  * dry-run is the default and never calls X.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.database import get_engine
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import (
    DRAFT_APPROVED,
    DRAFT_BLOCKED,
    DRAFT_NEEDS_RECONCILIATION,
    DRAFT_PUBLISHED,
    MEMORY_CONVERSATION,
    MEMORY_EPISODIC,
    Account,
    ConversationThread,
    Draft,
    Interaction,
    OptOutRecord,
    Post,
    PublishingJob,
    SourceDocument,
)
from app.memory.repositories import AuditRepository, PostRepository
from app.memory.vector_store import MemoryVectorStore
from app.publishing.policy_gate import evaluate_original_post, evaluate_reply
from app.publishing.provider import PublishingProvider, get_publishing_provider
from app.publishing.x_client import (
    AmbiguousPublishError,
    XAuthError,
    XError,
    XNotConfiguredError,
    XPermissionError,
    XRateLimitedError,
    XTransientError,
    XValidationError,
)
from app.schemas.publishing import PublishResult

logger = get_logger(__name__)


class Publisher:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        x_client=None,  # XClient protocol: create_post/create_post only for this path
        *,
        embedder: EmbeddingProvider | None = None,
        provider: PublishingProvider | None = None,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.x_client = x_client
        self.embedder = embedder
        self.clock = clock or (lambda: datetime.now(UTC))
        self.store = MemoryVectorStore(settings)
        self.provider = provider or get_publishing_provider(
            settings, session_factory, x_client, clock=self.clock
        )

    def _parts(self, draft: Draft) -> list[str]:
        return [draft.body, *[part for part in (draft.thread_parts or []) if part]]

    async def _existing_posts(self, session: AsyncSession, draft_id: int) -> list[Post]:
        return list(
            (
                await session.scalars(
                    select(Post).where(Post.draft_id == draft_id).order_by(Post.position)
                )
            ).all()
        )

    async def _is_trending(self, session: AsyncSession, draft: Draft) -> bool:
        if (draft.policy_report or {}).get("trending"):
            return True
        ids = draft.source_document_ids or []
        if not ids:
            return False
        rows = await session.scalars(select(SourceDocument).where(SourceDocument.id.in_(ids)))
        return any(bool(doc.metadata_.get("trending")) for doc in rows)

    def _configured(self) -> bool:
        return bool(getattr(self.x_client, "configured", True))

    async def _index_post(
        self, session: AsyncSession, account_id: int, post: Post
    ) -> None:
        if self.embedder is None:
            return
        await self.store.add_memory(
            session,
            account_id=account_id,
            category=MEMORY_EPISODIC,
            record_type="published_post",
            content=post.text,
            summary=f"Published post {post.x_post_id}",
            post_id=post.id,
            confidence=1.0,
            provenance={
                "x_post_id": post.x_post_id,
                "url": post.url,
                "published_at": post.published_at.isoformat(),
                "source": "publisher",
            },
            embedding_provider=self.embedder,
        )

    async def publish_original(self, draft_id: int) -> PublishResult:
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            if draft is None:
                raise ValueError(f"draft {draft_id} not found")
            if draft.kind != "original":
                raise ValueError("publish_original only accepts original drafts")
            account = await session.get(Account, draft.account_id)
            existing = await self._existing_posts(session, draft_id)
            parts = self._parts(draft)
            if len(existing) >= len(parts):
                return PublishResult(
                    status="already_published",
                    draft_id=draft.id,
                    post_ids=[post.x_post_id or "" for post in existing],
                )
            if draft.status == DRAFT_PUBLISHED:
                return PublishResult(
                    status="already_published",
                    draft_id=draft.id,
                    post_ids=[post.x_post_id or "" for post in existing],
                )
            if draft.status == DRAFT_NEEDS_RECONCILIATION:
                return PublishResult(
                    status="unknown",
                    draft_id=draft.id,
                    post_ids=[post.x_post_id or "" for post in existing],
                    reasons=[
                        "a previous attempt had an ambiguous outcome; reconcile manually "
                        "before publishing"
                    ],
                )
            now = self.clock()
            posts_today = await PostRepository.count_published_since(
                session, draft.account_id, now - timedelta(days=1), kind="original"
            )
            last_published = await PostRepository.last_published_at(session, draft.account_id)
            trending = await self._is_trending(session, draft)
            decision = evaluate_original_post(
                settings=self.settings,
                draft=draft,
                account_active=bool(account and account.is_active),
                posts_today=posts_today,
                last_published_at=last_published,
                now=now,
                trending_topic=trending,
                already_published=bool(existing),
            )
            await AuditRepository.log(
                session,
                action="publish_original",
                outcome="allowed" if decision.allowed else "blocked",
                account_id=draft.account_id,
                details={
                    "draft_id": draft.id,
                    "hard_failures": decision.hard_failures,
                    "soft_failures": decision.soft_failures,
                },
            )
            policy = (draft.policy_report or {})
            policy["gate"] = decision.model_dump(mode="json")
            draft.policy_report = policy
            if not decision.allowed:
                if decision.hard_failures:
                    draft.status = DRAFT_BLOCKED
                    draft.error = "; ".join(decision.hard_failures)[:4000]
                await session.commit()
                return PublishResult(
                    status="blocked" if decision.hard_failures else "skipped",
                    draft_id=draft.id,
                    reasons=decision.hard_failures or decision.soft_failures,
                    policy=decision,
                )

            # Strict ₹0 Zero-Cost Mode: Route to LocalDraftProvider immediately.
            # Never connects to api.x.com or initializes an X OAuth client.
            if self.settings.is_zero_cost_mode or self.settings.x_publishing_mode == "local_draft":
                await session.commit()
                return await self.provider.publish(draft, parts, decision=decision)

            if self.settings.dry_run:
                await self._record_job(
                    session, "simulated", draft, post_ids=[], extra={"dry_run": True}
                )
                await AuditRepository.log(
                    session,
                    action="publish_original",
                    outcome="simulated",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "parts": len(parts)},
                )
                await session.commit()
                return PublishResult(
                    status="simulated", draft_id=draft.id, reasons=["dry run"], policy=decision
                )
            if not self._configured():
                await session.commit()
                return PublishResult(
                    status="failed",
                    draft_id=draft.id,
                    reasons=["X client is not configured"],
                    policy=decision,
                )
            await session.commit()

        # Advisory lock is held on a dedicated connection; it is released even on error.
        lock_conn = await get_engine().connect()
        got_lock = False
        try:
            got_lock = bool(
                await lock_conn.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": draft_id}
                )
            )
            if not got_lock:
                return PublishResult(
                    status="skipped",
                    draft_id=draft_id,
                    reasons=["another worker holds the publish lock"],
                )
            return await self._publish_live(draft_id)
        finally:
            if got_lock:
                await lock_conn.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": draft_id}
                )
            await lock_conn.close()

    async def _record_job(
        self,
        session: AsyncSession,
        status: str,
        draft: Draft,
        *,
        post_ids: list[str],
        extra: dict[str, Any] | None = None,
        attempt: int = 0,
        kind: str = "original",
    ) -> PublishingJob:
        key = f"publish:{kind}:{draft.id}"
        job = await session.scalar(
            select(PublishingJob).where(PublishingJob.idempotency_key == key)
        )
        if job is None:
            job = PublishingJob(
                account_id=draft.account_id,
                draft_id=draft.id,
                kind="original",
                status=status,
                idempotency_key=key,
                attempts=attempt or 1,
                started_at=self.clock(),
            )
            session.add(job)
        job.status = status
        job.attempts = max(job.attempts, 1)
        job.finished_at = self.clock() if status != "running" else None
        result = {"post_ids": post_ids, **(extra or {})}
        job.result = result
        await session.flush()
        return job

    async def _publish_live(self, draft_id: int) -> PublishResult:
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            if draft is None:
                raise ValueError(f"draft {draft_id} not found")
            parts = self._parts(draft)
            existing = await self._existing_posts(session, draft_id)
            positions = {post.position for post in existing}
            previous_id = existing[-1].x_post_id if existing else None
            started_at = self.clock()
            job = await self._record_job(
                session, "running", draft, post_ids=[p.x_post_id or "" for p in existing]
            )
            job.started_at = started_at
            await session.commit()

            published_ids: list[str] = [p.x_post_id or "" for p in existing]
            try:
                for position, part in enumerate(parts):
                    if position in positions:
                        continue
                    result = await self.x_client.create_post(part, in_reply_to=previous_id)
                    published_ids.append(result.id)
                    post = await PostRepository.create(
                        session,
                        account_id=draft.account_id,
                        draft_id=draft.id,
                        x_post_id=result.id,
                        kind="original",
                        position=position,
                        text=part,
                        url=result.url,
                        published_at=self.clock(),
                        raw=result.raw,
                    )
                    await self._index_post(session, draft.account_id, post)
                    previous_id = result.id
                    await session.commit()
            except AmbiguousPublishError as exc:
                job.status = "unknown"
                job.finished_at = self.clock()
                job.last_error = str(exc)[:1000]
                draft.status = DRAFT_NEEDS_RECONCILIATION
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session,
                    action="publish_original",
                    outcome="unknown",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "error": str(exc)[:300],
                             "post_ids": published_ids},
                )
                await session.commit()
                return PublishResult(
                    status="unknown", draft_id=draft.id, post_ids=published_ids,
                    reasons=[str(exc)],
                )
            except (XRateLimitedError, XTransientError, XPermissionError, XAuthError,
                    XNotConfiguredError) as exc:
                job.status = "failed"
                job.finished_at = self.clock()
                job.last_error = str(exc)[:1000]
                if published_ids:
                    draft.status = "failed"
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session,
                    action="publish_original",
                    outcome="failed",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "error": str(exc)[:300],
                             "post_ids": published_ids,
                             "retryable": True},
                )
                await session.commit()
                return PublishResult(
                    status="failed", draft_id=draft.id, post_ids=published_ids,
                    reasons=[str(exc)],
                )
            except XValidationError as exc:
                job.status = "failed"
                job.finished_at = self.clock()
                job.last_error = str(exc)[:1000]
                draft.status = DRAFT_BLOCKED
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session,
                    action="publish_original",
                    outcome="blocked",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "error": str(exc)[:300]},
                )
                await session.commit()
                return PublishResult(
                    status="blocked", draft_id=draft.id, post_ids=published_ids,
                    reasons=[str(exc)],
                )
            except XError as exc:
                job.status = "failed"
                job.finished_at = self.clock()
                job.last_error = str(exc)[:1000]
                await AuditRepository.log(
                    session,
                    action="publish_original",
                    outcome="failed",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "error": str(exc)[:300]},
                )
                await session.commit()
                return PublishResult(
                    status="failed", draft_id=draft.id, post_ids=published_ids,
                    reasons=[str(exc)],
                )

            job.status = "succeeded"
            job.finished_at = self.clock()
            job.result = {"post_ids": published_ids}
            draft.status = DRAFT_PUBLISHED
            draft.published_at = self.clock()
            draft.error = None
            await AuditRepository.log(
                session,
                action="publish_original",
                outcome="published",
                account_id=draft.account_id,
                details={"draft_id": draft.id, "post_ids": published_ids},
            )
            await session.commit()
            logger.info(
                "draft published",
                extra={"extra_fields": {"draft_id": draft.id, "post_ids": published_ids}},
            )
            return PublishResult(
                status="published", draft_id=draft.id, post_ids=published_ids
            )

    # ------------------------------------------------------------------
    # replies: internal only, never exposed as an HTTP action
    # ------------------------------------------------------------------
    async def publish_reply(self, draft_id: int) -> PublishResult:
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            if draft is None:
                raise ValueError(f"draft {draft_id} not found")
            if draft.kind != "reply":
                raise ValueError("publish_reply only accepts reply drafts")
            parts = self._parts(draft)
            existing = list(
                (
                    await session.scalars(select(Post).where(Post.draft_id == draft.id))
                ).all()
            )
            if draft.status == DRAFT_PUBLISHED or existing:
                return PublishResult(
                    status="already_published",
                    draft_id=draft.id,
                    post_ids=[post.x_post_id or "" for post in existing],
                )
            if draft.status == DRAFT_NEEDS_RECONCILIATION:
                return PublishResult(
                    status="unknown",
                    draft_id=draft.id,
                    reasons=["ambiguous previous attempt; reconcile manually"],
                )
            if draft.status != DRAFT_APPROVED:
                return PublishResult(
                    status="blocked",
                    draft_id=draft.id,
                    reasons=[f"draft status is {draft.status!r}; reviewer approval required"],
                )
            if not draft.in_reply_to_post_id:
                return PublishResult(
                    status="blocked", draft_id=draft.id, reasons=["missing target post id"]
                )

            interaction = await session.scalar(
                select(Interaction).where(
                    Interaction.account_id == draft.account_id,
                    Interaction.x_post_id == draft.in_reply_to_post_id,
                    Interaction.direction == "inbound",
                )
            )
            opt_out = None
            if draft.target_author_id:
                opt_out = await session.scalar(
                    select(OptOutRecord).where(
                        OptOutRecord.account_id == draft.account_id,
                        OptOutRecord.x_user_id == draft.target_author_id,
                    )
                )
            thread = (
                await session.get(ConversationThread, draft.conversation_thread_id)
                if draft.conversation_thread_id
                else None
            )
            conversation_rows = []
            if thread is not None:
                conversation_rows = list(
                    (
                        await session.scalars(
                            select(Interaction).where(
                                Interaction.conversation_thread_id == thread.id
                            )
                        )
                    ).all()
                )
            replies_today = await PostRepository.count_published_since(
                session, draft.account_id, self.clock() - timedelta(days=1), kind="reply"
            )
            decision = evaluate_reply(
                settings=self.settings,
                draft=draft,
                account_active=True,
                replies_today=replies_today,
                has_inbound_interaction=interaction is not None,
                conversation_has_context=bool(
                    conversation_rows and any(row.text.strip() for row in conversation_rows)
                ),
                opt_out=opt_out is not None,
                now=self.clock(),
            )
            await AuditRepository.log(
                session,
                action="publish_reply",
                outcome="allowed" if decision.allowed else "blocked",
                account_id=draft.account_id,
                details={"draft_id": draft.id, "hard_failures": decision.hard_failures,
                         "soft_failures": decision.soft_failures},
            )
            policy = dict(draft.policy_report or {})
            policy["gate"] = decision.model_dump(mode="json")
            draft.policy_report = policy
            if not decision.allowed:
                if decision.hard_failures:
                    draft.status = DRAFT_BLOCKED
                    draft.error = "; ".join(decision.hard_failures)[:4000]
                await session.commit()
                return PublishResult(
                    status="blocked" if decision.hard_failures else "skipped",
                    draft_id=draft.id,
                    reasons=decision.hard_failures or decision.soft_failures,
                    policy=decision,
                )

            # Strict ₹0 Zero-Cost Mode: Route to LocalDraftProvider immediately.
            # Never connects to api.x.com or initializes an X OAuth client.
            if self.settings.is_zero_cost_mode or self.settings.x_publishing_mode == "local_draft":
                await session.commit()
                return await self.provider.publish(draft, parts, decision=decision)

            if self.settings.dry_run:
                await self._record_job(
                    session, "simulated", draft, post_ids=[], kind="reply",
                    extra={"dry_run": True},
                )
                await AuditRepository.log(
                    session, action="publish_reply", outcome="simulated",
                    account_id=draft.account_id, details={"draft_id": draft.id},
                )
                await session.commit()
                return PublishResult(
                    status="simulated", draft_id=draft.id, reasons=["dry run"], policy=decision
                )
            if not self._configured():
                await session.commit()
                return PublishResult(
                    status="failed", draft_id=draft.id, reasons=["X client is not configured"],
                    policy=decision,
                )
            await session.commit()

        lock_conn = await get_engine().connect()
        got_lock = False
        try:
            got_lock = bool(
                await lock_conn.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": 1_000_000 + draft_id}
                )
            )
            if not got_lock:
                return PublishResult(
                    status="skipped", draft_id=draft_id,
                    reasons=["another worker holds the reply publish lock"],
                )
            return await self._publish_reply_live(draft_id)
        finally:
            if got_lock:
                await lock_conn.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": 1_000_000 + draft_id}
                )
            await lock_conn.close()

    async def _publish_reply_live(self, draft_id: int) -> PublishResult:
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            if draft is None:
                raise ValueError(f"draft {draft_id} not found")
            await self._record_job(session, "running", draft, post_ids=[], kind="reply")
            await session.commit()
            parts = self._parts(draft)
            previous = draft.in_reply_to_post_id
            published_ids: list[str] = []
            try:
                for position, part in enumerate(parts):
                    result = await self.x_client.create_post(part, in_reply_to=previous)
                    published_ids.append(result.id)
                    post = await PostRepository.create(
                        session,
                        account_id=draft.account_id,
                        draft_id=draft.id,
                        x_post_id=result.id,
                        kind="reply",
                        position=position,
                        text=part,
                        url=result.url,
                        conversation_id=(
                            str((draft.policy_report or {}).get("conversation_id", "")) or None
                        ),
                        in_reply_to_post_id=previous,
                        published_at=self.clock(),
                        raw=result.raw,
                    )
                    if self.embedder is not None:
                        await self.store.add_memory(
                            session,
                            account_id=draft.account_id,
                            category=MEMORY_CONVERSATION,
                            record_type="our_reply",
                            content=part,
                            conversation_thread_id=draft.conversation_thread_id,
                            post_id=post.id,
                            confidence=1.0,
                            provenance={"x_post_id": result.id, "url": result.url,
                                        "in_reply_to": previous},
                            embedding_provider=self.embedder,
                        )
                    previous = result.id
                    await session.commit()
                thread = (
                    await session.get(ConversationThread, draft.conversation_thread_id)
                    if draft.conversation_thread_id
                    else None
                )
                if thread is not None:
                    thread.last_activity_at = self.clock()
                job = await self._record_job(
                    session, "succeeded", draft, post_ids=published_ids, kind="reply"
                )
                job.result = {"post_ids": published_ids}
                draft.status = DRAFT_PUBLISHED
                draft.published_at = self.clock()
                draft.error = None
                await AuditRepository.log(
                    session, action="publish_reply", outcome="published",
                    account_id=draft.account_id,
                    details={"draft_id": draft.id, "post_ids": published_ids},
                )
                await session.commit()
            except AmbiguousPublishError as exc:
                job = await self._record_job(session, "unknown", draft, post_ids=published_ids,
                                             kind="reply")
                job.last_error = str(exc)[:1000]
                draft.status = DRAFT_NEEDS_RECONCILIATION
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session, action="publish_reply", outcome="unknown",
                    account_id=draft.account_id, details={"draft_id": draft.id},
                )
                await session.commit()
                return PublishResult(status="unknown", draft_id=draft.id,
                                     post_ids=published_ids, reasons=[str(exc)])
            except (XRateLimitedError, XTransientError, XPermissionError, XAuthError,
                    XNotConfiguredError) as exc:
                job = await self._record_job(session, "failed", draft, post_ids=published_ids,
                                             kind="reply")
                job.last_error = str(exc)[:1000]
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session, action="publish_reply", outcome="failed",
                    account_id=draft.account_id, details={"draft_id": draft.id,
                                                         "error": str(exc)[:300]},
                )
                await session.commit()
                return PublishResult(status="failed", draft_id=draft.id,
                                     post_ids=published_ids, reasons=[str(exc)])
            except XValidationError as exc:
                job = await self._record_job(session, "failed", draft, post_ids=published_ids,
                                             kind="reply")
                job.last_error = str(exc)[:1000]
                draft.status = DRAFT_BLOCKED
                draft.error = str(exc)[:4000]
                await AuditRepository.log(
                    session, action="publish_reply", outcome="blocked",
                    account_id=draft.account_id, details={"draft_id": draft.id},
                )
                await session.commit()
                return PublishResult(status="blocked", draft_id=draft.id,
                                     post_ids=published_ids, reasons=[str(exc)])
            return PublishResult(status="published", draft_id=draft.id, post_ids=published_ids)
