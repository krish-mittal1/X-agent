"""Growth plan API: today's schedule, the action queue, and recorded actions.

Read-only for the plan; the only write is recording an action the operator
actually took, which feeds the coordination guard. Nothing here publishes.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AdminAuth, DbSession, SettingsDep
from app.growth.engagement import EngagementService
from app.growth.service import GrowthService
from app.memory.database import get_session_factory
from app.memory.repositories import AccountRepository
from app.providers.factory import LLMService, build_provider
from app.publishing.brave_cdp import BraveCDPPublisher

router = APIRouter(prefix="/growth", tags=["growth"])


class RecordActionRequest(BaseModel):
    kind: Literal["post", "reply", "follow", "like"]
    target_handle: str = Field(default="", max_length=64)
    target_x_user_id: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=280)


class GenerateBangersRequest(BaseModel):
    target_post_url: str = Field(default="")
    target_text: str = Field(default="", max_length=2000)
    target_author: str = Field(default="", max_length=64)
    context: str = Field(default="", max_length=500)


class DispatchEngagementRequest(BaseModel):
    target_post_url: str
    content: str = Field(default="", max_length=300)
    action: Literal["reply", "quote", "retweet"] = "reply"
    style: str = "sarcastic"


async def _service(settings: SettingsDep, db: DbSession) -> tuple[GrowthService, int]:
    account = await AccountRepository.get_default(db, settings.x_persona_handle)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no active account")
    return GrowthService(settings, get_session_factory()), account.id


@router.get("/plan")
async def get_plan(settings: SettingsDep, _: AdminAuth, db: DbSession) -> dict[str, object]:
    account = await AccountRepository.get_default(db, settings.x_persona_handle)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no active account")
    service = GrowthService(settings, get_session_factory())
    return (await service.build(account)).to_dict()


@router.get("/actions")
async def list_actions(
    settings: SettingsDep,
    _: AdminAuth,
    db: DbSession,
    limit: int = 50,
    kind: str | None = None,
) -> dict[str, object]:
    service, account_id = await _service(settings, db)
    records = await service.recent_records(account_id, limit=limit, kind=kind)
    return {"count": len(records), "actions": list(records)}


@router.post("/actions", status_code=status.HTTP_201_CREATED)
async def record_action(
    payload: RecordActionRequest, settings: SettingsDep, _: AdminAuth, db: DbSession
) -> dict[str, object]:
    service, account_id = await _service(settings, db)
    return await service.record_action(
        account_id=account_id,
        kind=payload.kind,
        target_handle=payload.target_handle,
        target_x_user_id=payload.target_x_user_id,
        note=payload.note,
    )


@router.post("/engage/generate")
async def generate_bangers_endpoint(
    payload: GenerateBangersRequest,
    settings: SettingsDep,
    _: AdminAuth,
    db: DbSession,
) -> dict[str, Any]:
    """Generate 3 viral engagement takes (sarcastic, value-add, quote-tweet) for any target tweet."""
    account = await AccountRepository.get_default(db, settings.x_persona_handle)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no active account")

    provider = build_provider(settings)
    session_factory = get_session_factory()
    llm = LLMService(settings, provider, session_factory)
    try:
        service = EngagementService(settings, llm, session_factory)
        target_text = payload.target_text
        target_author = payload.target_author

        if payload.target_post_url and not target_text:
            publisher = BraveCDPPublisher(settings, session_factory)
            details = await publisher.fetch_tweet_details(payload.target_post_url)
            target_text = details.get("text", "")
            target_author = details.get("author", target_author)

        if not target_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="target_text or resolvable target_post_url required",
            )

        bangers = await service.generate_bangers(
            target_text=target_text,
            target_author=target_author,
            context=payload.context,
        )
        return {
            "target_post_url": payload.target_post_url,
            "target_author": target_author,
            "target_text": target_text,
            "bangers": bangers,
        }
    finally:
        await llm.aclose()


@router.post("/engage/dispatch")
async def dispatch_engagement_endpoint(
    payload: DispatchEngagementRequest,
    settings: SettingsDep,
    _: AdminAuth,
    db: DbSession,
) -> dict[str, Any]:
    """Dispatch a reply, quote tweet, or retweet directly to X via Brave CDP."""
    account = await AccountRepository.get_default(db, settings.x_persona_handle)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no active account")

    session_factory = get_session_factory()
    publisher = BraveCDPPublisher(settings, session_factory)

    if payload.action == "retweet":
        return await publisher.repost(payload.target_post_url)

    provider = build_provider(settings)
    llm = LLMService(settings, provider, session_factory)
    try:
        service = EngagementService(settings, llm, session_factory, publisher=publisher)
        draft = await service.create_engagement_draft(
            account_id=account.id,
            target_post_url=payload.target_post_url,
            content=payload.content,
            action="quote" if payload.action == "quote" else "reply",
            style=payload.style,
            operator_approved=True,
        )
        result = await service.publish_engagement(draft.id)
        return {
            "status": result.status,
            "draft_id": draft.id,
            "action": payload.action,
            "post_ids": result.post_ids,
            "reasons": result.reasons,
        }
    finally:
        await llm.aclose()
