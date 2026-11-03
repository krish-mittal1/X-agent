"""Authenticated draft review API. There is deliberately no publish endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminAuth, DbSession, SettingsDep
from app.memory.models import Account, Draft, EvidenceClaim
from app.memory.repositories import DraftRepository
from app.publishing.reply_queue import ReplyQueue, ReviewError

router = APIRouter(prefix="/drafts", tags=["drafts"])


class ApproveRequest(BaseModel):
    reviewer: str = Field(default="admin", min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    reviewer: str = Field(default="admin", min_length=1, max_length=128)
    notes: str = Field(default="Discarded by operator", min_length=1, max_length=2000)


def _serialize(draft: Draft, *, detail: bool = False) -> dict[str, Any]:
    image_url = None
    if draft.policy_report and isinstance(draft.policy_report, dict):
        image_url = draft.policy_report.get("image_url")
    if not image_url and draft.verification_report and isinstance(draft.verification_report, dict):
        image_url = draft.verification_report.get("image_url")

    policy: dict[str, Any] = draft.policy_report if isinstance(draft.policy_report, dict) else {}
    critic_raw = policy.get("critic")
    critic: dict[str, Any] = critic_raw if isinstance(critic_raw, dict) else {}
    alts_raw = policy.get("alternative_angles")
    alts: list[Any] = alts_raw if isinstance(alts_raw, list) else []

    payload: dict[str, Any] = {
        "id": draft.id,
        "kind": draft.kind,
        "status": draft.status,
        "topic": draft.topic,
        "format": draft.tone,
        "body": draft.body,
        "thread_parts": draft.thread_parts,
        "rationale": draft.rationale,
        "image_url": image_url,
        "image_verified": bool(policy.get("image_verified")),
        "image_source_url": policy.get("image_source_url"),
        "image_reason": policy.get("image_reason"),
        "quality_score": critic.get("overall_score") or draft.candidate_score,
        "critic_verdict": critic.get("verdict"),
        "alternative_angles_count": len(alts),
        "alternative_angles": alts,
        "sources_count": len(draft.source_document_ids or []),
        "verification_passed": (
            (draft.verification_report or {}).get("passed", True)
            if draft.verification_report
            else None
        ),
        "event_id": policy.get("event_id"),
        "event_title": policy.get("event_title"),
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "target_author_handle": draft.target_author_handle,
        "in_reply_to_post_id": draft.in_reply_to_post_id,
        "reviewed_by": draft.reviewed_by,
        "reviewed_at": draft.reviewed_at.isoformat() if draft.reviewed_at else None,
        "review_notes": draft.review_notes,
        "error": draft.error,
    }
    if detail:
        payload.update(
            {
                "verification": draft.verification_report,
                "policy": draft.policy_report,
                "alternative_angles": alts,
                "critic": critic,
                "source_document_ids": draft.source_document_ids,
                "conversation_thread_id": draft.conversation_thread_id,
                "published_at": draft.published_at.isoformat() if draft.published_at else None,
            }
        )
    return payload


async def resolve_account(session: AsyncSession, handle: str) -> Account:
    """Single-account administration: configured handle, else the first active account."""
    if handle:
        account = await session.scalar(
            select(Account).where(Account.handle == handle.strip().lstrip("@").lower())
        )
        if account is not None:
            return account
    account = await session.scalar(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no active account configured; run scripts/bootstrap.py first",
        )
    return account


@router.get("")
async def list_drafts(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    kind: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    drafts, total = await DraftRepository.list_paginated(
        db, account.id, status=status_filter, kind=kind, limit=limit, offset=offset
    )
    return {
        "items": [_serialize(draft) for draft in drafts],
        "total": total,
        "limit": limit,
        "offset": offset,
        "account": account.handle,
    }


@router.get("/{draft_id}")
async def get_draft(
    draft_id: int, _: AdminAuth, db: DbSession, settings: SettingsDep
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    draft = await DraftRepository.get(db, account.id, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    claims = list(
        (
            await db.scalars(select(EvidenceClaim).where(EvidenceClaim.draft_id == draft.id))
        ).all()
    )
    payload = _serialize(draft, detail=True)
    payload["claims"] = [
        {
            "claim": claim.claim_text,
            "type": claim.claim_type,
            "status": claim.status,
            "confidence": claim.confidence,
            "source_url": claim.source_url,
            "notes": claim.notes,
        }
        for claim in claims
    ]
    return payload


@router.post("/{draft_id}/approve")
async def approve_draft(
    draft_id: int,
    body: ApproveRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    try:
        draft = await ReplyQueue.approve(
            db, account.id, draft_id, reviewer=body.reviewer, notes=body.notes
        )
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return _serialize(draft, detail=True)


@router.post("/{draft_id}/reject")
async def reject_draft(
    draft_id: int,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    body: RejectRequest | None = None,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    req = body or RejectRequest()
    try:
        draft = await ReplyQueue.reject(
            db, account.id, draft_id, reviewer=req.reviewer, notes=req.notes
        )
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return _serialize(draft, detail=True)
