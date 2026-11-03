"""Human review queue for reply drafts.

Approval changes state only. It never sends anything; sending is a separate,
configuration-gated operation that is not exposed over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.models import (
    DRAFT_APPROVED,
    DRAFT_NEEDS_REVIEW,
    DRAFT_PUBLISHED,
    DRAFT_READY,
    DRAFT_REJECTED,
    Draft,
)
from app.memory.repositories import AuditRepository, DraftRepository


class ReviewError(ValueError):
    """Invalid review transition (reported to the caller as 409)."""


class ReplyQueue:
    @staticmethod
    async def list(
        session: AsyncSession,
        account_id: int,
        *,
        status: str | None = "needs_review",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Draft], int]:
        return await DraftRepository.list_paginated(
            session, account_id, status=status, kind="reply", limit=limit, offset=offset
        )

    @staticmethod
    async def get(session: AsyncSession, account_id: int, draft_id: int) -> Draft | None:
        return await DraftRepository.get(session, account_id, draft_id)

    @staticmethod
    async def approve(
        session: AsyncSession,
        account_id: int,
        draft_id: int,
        *,
        reviewer: str,
        notes: str | None = None,
    ) -> Draft:
        draft = await DraftRepository.get(session, account_id, draft_id)
        if draft is None:
            raise ReviewError("draft not found")
        if draft.status == DRAFT_PUBLISHED:
            raise ReviewError("draft already published")
        if draft.kind != "reply":
            raise ReviewError("only reply drafts use the review queue")
        if draft.status != DRAFT_NEEDS_REVIEW:
            raise ReviewError(f"draft is {draft.status!r}, not awaiting review")
        draft.status = DRAFT_APPROVED
        draft.reviewed_by = reviewer[:128]
        draft.reviewed_at = datetime.now(UTC)
        draft.review_notes = (notes or "")[:2000] or None
        await AuditRepository.log(
            session,
            action="reply_draft_review",
            outcome="approved",
            account_id=account_id,
            details={"draft_id": draft.id, "reviewer": reviewer},
        )
        await session.flush()
        return draft

    @staticmethod
    async def reject(
        session: AsyncSession,
        account_id: int,
        draft_id: int,
        *,
        reviewer: str,
        notes: str,
    ) -> Draft:
        if not notes or len(notes.strip()) < 3:
            raise ReviewError("rejection requires a reason")
        draft = await DraftRepository.get(session, account_id, draft_id)
        if draft is None:
            raise ReviewError("draft not found")
        if draft.status == DRAFT_PUBLISHED:
            raise ReviewError("draft already published")
        if draft.status not in {DRAFT_READY, DRAFT_NEEDS_REVIEW, DRAFT_APPROVED}:
            raise ReviewError(f"draft is {draft.status!r} and cannot be rejected")
        draft.status = DRAFT_REJECTED
        draft.reviewed_by = reviewer[:128]
        draft.reviewed_at = datetime.now(UTC)
        draft.review_notes = notes.strip()[:2000]
        await AuditRepository.log(
            session,
            action="reply_draft_review",
            outcome="rejected",
            account_id=account_id,
            details={"draft_id": draft.id, "reviewer": reviewer, "reason": notes[:200]},
        )
        await session.flush()
        return draft
