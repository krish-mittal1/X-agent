"""Database repositories (thin, explicit, account-scoped)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.source_registry import classify_source
from app.logging_config import correlation_id_var
from app.memory.models import (
    Account,
    ActionLog,
    Draft,
    JobRun,
    Post,
    PublishingJob,
    SourceDocument,
)
from app.net import content_hash, normalize_url, url_hash
from app.schemas.research import CandidateScore, IngestedItem
from app.security import detect_prompt_injection


class AccountRepository:
    @staticmethod
    async def ensure_account(
        session: AsyncSession, handle: str, x_user_id: str | None = None
    ) -> Account:
        handle = handle.strip().lstrip("@").lower()
        if not handle:
            raise ValueError("account handle is required")
        account = await session.scalar(select(Account).where(Account.handle == handle))
        if account is None:
            account = Account(handle=handle, x_user_id=x_user_id, display_name=handle)
            session.add(account)
            await session.flush()
        elif x_user_id and not account.x_user_id:
            account.x_user_id = x_user_id
        return account

    @staticmethod
    async def get(session: AsyncSession, account_id: int) -> Account | None:
        return await session.get(Account, account_id)

    @staticmethod
    async def get_default(session: AsyncSession, handle: str = "") -> Account | None:
        normalized = handle.strip().lstrip("@").lower()
        if normalized:
            account = await session.scalar(
                select(Account).where(Account.handle == normalized)
            )
            if account is not None:
                return account
        return await session.scalar(
            select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
        )


class SourceRepository:
    @staticmethod
    async def get(session: AsyncSession, account_id: int, source_id: int) -> SourceDocument | None:
        return await session.scalar(
            select(SourceDocument).where(
                SourceDocument.id == source_id, SourceDocument.account_id == account_id
            )
        )

    @staticmethod
    async def store_item(
        session: AsyncSession, account_id: int, item: IngestedItem
    ) -> tuple[SourceDocument | None, bool]:
        """Persist an ingested item. Returns (document, created).

        Dedupe is by canonical URL and by content hash; the URL unique constraint
        is the hard guarantee, the content check catches syndicated copies.
        """
        try:
            canonical = normalize_url(item.url)
        except ValueError:
            return None, False
        digest_url = url_hash(canonical)
        digest_content = content_hash(f"{item.title}\n{item.content}")

        existing = await session.scalar(
            select(SourceDocument).where(
                SourceDocument.account_id == account_id,
                (SourceDocument.url_hash == digest_url)
                | (SourceDocument.content_hash == digest_content),
            )
        )
        if existing is not None:
            return existing, False

        profile = classify_source(item.source_type, canonical)
        injection_markers = detect_prompt_injection(f"{item.title}\n{item.content}")
        document = SourceDocument(
            account_id=account_id,
            source_type=item.source_type,
            source_identifier=item.source_identifier[:512],
            url=canonical,
            url_hash=digest_url,
            canonical_url=canonical,
            title=item.title[:1000],
            author=(item.author or None),
            published_at=item.published_at,
            retrieved_at=datetime.now(UTC),
            content=item.content,
            content_hash=digest_content,
            excerpt=item.excerpt,
            reliability_score=item.reliability_score or profile.reliability,
            fetch_status=item.fetch_status,
            fetch_error=item.fetch_error,
            metadata_={
                **item.metadata,
                "external_id": item.external_id,
                "is_primary_source": item.metadata.get("is_primary_source", profile.is_primary),
                "injection_suspected": bool(injection_markers),
                "injection_markers": injection_markers,
            },
        )
        try:
            async with session.begin_nested():
                session.add(document)
                await session.flush()
        except IntegrityError:
            return None, False
        return document, True

    @staticmethod
    async def list_unscored(
        session: AsyncSession, account_id: int, *, cutoff: datetime, limit: int = 50
    ) -> list[SourceDocument]:
        stmt = (
            select(SourceDocument)
            .where(
                SourceDocument.account_id == account_id,
                SourceDocument.scored_at.is_(None),
                SourceDocument.fetch_status != "failed",
                func.coalesce(SourceDocument.published_at, SourceDocument.retrieved_at) >= cutoff,
            )
            .order_by(SourceDocument.retrieved_at.desc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def ranked_candidates(
        session: AsyncSession,
        account_id: int,
        *,
        min_score: float,
        limit: int,
        cutoff: datetime,
    ) -> list[SourceDocument]:
        stmt = (
            select(SourceDocument)
            .where(
                SourceDocument.account_id == account_id,
                SourceDocument.processed_at.is_(None),
                SourceDocument.fetch_status != "failed",
                SourceDocument.candidate_score.is_not(None),
                SourceDocument.candidate_score >= min_score,
                func.coalesce(SourceDocument.published_at, SourceDocument.retrieved_at) >= cutoff,
            )
            .order_by(SourceDocument.candidate_score.desc(), SourceDocument.retrieved_at.desc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def apply_score(
        session: AsyncSession, document: SourceDocument, score: CandidateScore
    ) -> None:
        document.candidate_score = score.score
        document.score_breakdown = score.model_dump(mode="json")
        document.scored_at = datetime.now(UTC)

    @staticmethod
    async def mark_processed(
        session: AsyncSession, account_id: int, source_ids: list[int]
    ) -> None:
        if not source_ids:
            return
        for source_id in source_ids:
            doc = await SourceRepository.get(session, account_id, source_id)
            if doc is not None:
                doc.processed_at = datetime.now(UTC)

    @staticmethod
    async def recent_titles(
        session: AsyncSession, account_id: int, limit: int = 30
    ) -> list[str]:
        stmt = (
            select(SourceDocument.title)
            .where(SourceDocument.account_id == account_id, SourceDocument.processed_at.is_not(None))
            .order_by(SourceDocument.retrieved_at.desc())
            .limit(limit)
        )
        return [row for row in (await session.scalars(stmt)).all() if row]


class DraftRepository:
    @staticmethod
    async def get(session: AsyncSession, account_id: int, draft_id: int) -> Draft | None:
        return await session.scalar(
            select(Draft).where(Draft.id == draft_id, Draft.account_id == account_id)
        )

    @staticmethod
    async def get_by_key(
        session: AsyncSession, account_id: int, idempotency_key: str
    ) -> Draft | None:
        return await session.scalar(
            select(Draft).where(
                Draft.account_id == account_id, Draft.idempotency_key == idempotency_key
            )
        )

    @staticmethod
    async def create(session: AsyncSession, **values: Any) -> Draft:
        draft = Draft(**values)
        session.add(draft)
        await session.flush()
        return draft

    @staticmethod
    async def list_paginated(
        session: AsyncSession,
        account_id: int,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Draft], int]:
        conditions = [Draft.account_id == account_id]
        if status:
            conditions.append(Draft.status == status)
        if kind:
            conditions.append(Draft.kind == kind)
        total = int(
            await session.scalar(select(func.count(Draft.id)).where(*conditions)) or 0
        )
        stmt = (
            select(Draft)
            .where(*conditions)
            .order_by(Draft.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.scalars(stmt)).all()), total


class PostRepository:
    @staticmethod
    async def count_published_since(
        session: AsyncSession, account_id: int, since: datetime, kind: str | None = None
    ) -> int:
        conditions = [Post.account_id == account_id, Post.published_at >= since]
        if kind:
            conditions.append(Post.kind == kind)
        return int(await session.scalar(select(func.count(Post.id)).where(*conditions)) or 0)

    @staticmethod
    async def last_published_at(session: AsyncSession, account_id: int) -> datetime | None:
        return await session.scalar(
            select(func.max(Post.published_at)).where(Post.account_id == account_id)
        )

    @staticmethod
    async def recent(session: AsyncSession, account_id: int, limit: int = 20) -> list[Post]:
        stmt = (
            select(Post)
            .where(Post.account_id == account_id)
            .order_by(Post.published_at.desc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def get_by_x_id(session: AsyncSession, x_post_id: str) -> Post | None:
        return await session.scalar(select(Post).where(Post.x_post_id == x_post_id))

    @staticmethod
    async def create(session: AsyncSession, **values: Any) -> Post:
        post = Post(**values)
        session.add(post)
        await session.flush()
        return post


class AuditRepository:
    @staticmethod
    async def log(
        session: AsyncSession,
        *,
        action: str,
        outcome: str,
        account_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> ActionLog:
        entry = ActionLog(
            account_id=account_id,
            action=action,
            outcome=outcome,
            correlation_id=correlation_id_var.get(),
            details=details or {},
        )
        session.add(entry)
        await session.flush()
        return entry


class PublishingJobRepository:
    @staticmethod
    async def get_by_key(session: AsyncSession, key: str) -> PublishingJob | None:
        return await session.scalar(
            select(PublishingJob).where(PublishingJob.idempotency_key == key)
        )


class JobRepository:
    @staticmethod
    async def begin(
        session: AsyncSession, job_name: str, run_key: str
    ) -> JobRun | None:
        """Insert a run marker; returns None if this run_key already started (idempotency)."""
        run = JobRun(job_name=job_name, run_key=run_key, status="running")
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError:
            return None
        return run

    @staticmethod
    async def finish(
        session: AsyncSession,
        run: JobRun,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.details = details or {}
