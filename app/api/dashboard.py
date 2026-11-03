"""Interactive Admin Dashboard and On-Demand Agent Command Controller.

Provides a browser-based UI and REST endpoints to:
  * Command the agent to research and draft tweets on specific topics on-demand.
  * Review, edit, approve, and instantly publish drafts to X.
  * Monitor autonomous doomscrolling, ingestion stats, and recent published posts.
  * Toggle publishing modes (live / dry-run / emergency pause) on the fly.
"""

from __future__ import annotations

import asyncio
import calendar
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import feedparser
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.persona import load_persona
from app.agent.reply_drafter import ReplyDrafter
from app.api.deps import AdminAuth, DbSession, SettingsDep
from app.api.drafts import _serialize, resolve_account
from app.config import Settings
from app.ingestion.extract import (
    clean_excerpt,
    extract_article_image,
    extract_text,
)
from app.ingestion.rss import _entry_datetime, _entry_html, _entry_image, parse_feed
from app.ingestion.source_registry import classify_source
from app.logging_config import get_logger
from app.memory.database import get_session_factory
from app.memory.embeddings import build_embedding_provider
from app.memory.models import (
    DRAFT_APPROVED,
    DRAFT_PUBLISHED,
    DRAFT_READY,
    Draft,
    Post,
    SourceDocument,
)
from app.memory.repositories import (
    AuditRepository,
    DraftRepository,
    PostRepository,
    SourceRepository,
)
from app.net import content_hash, normalize_url, validate_url_shape
from app.providers.base import LLMMessage
from app.providers.factory import LLMService, build_provider
from app.publishing.publisher import Publisher
from app.publishing.x_client import XClient
from app.research.scoring import CandidateScorer
from app.schemas.research import IngestedItem

logger = get_logger(__name__)

router = APIRouter(tags=["dashboard"])


class CommandDraftRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500, description="Topic, news event, or prompt for the tweet")
    format: str = Field(default="analysis", description="Post shape: analysis, breaking_jab, context, opinion")
    publish_now: bool = Field(default=False, description="Whether to publish immediately to X")


class UpdateDraftRequest(BaseModel):
    body: str = Field(min_length=5, max_length=500)
    image_url: str | None = None


class RefineDraftRequest(BaseModel):
    current_text: str = Field(min_length=5, max_length=1000)
    instruction: str = Field(min_length=2, max_length=500)


class DeepResearchRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=600, description="Topic hint or event description to research")
    format: str = Field(default="analysis", description="Post shape: analysis, breaking_jab, context, opinion")


class DeepResearchDraftRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=600, description="Original topic hint used for research")
    format: str = Field(default="analysis", description="Post shape: analysis, breaking_jab, context, opinion")
    # The operator-selected source to focus on
    selected_title: str = Field(default="", description="Title of the source the operator selected")
    selected_summary: str = Field(default="", description="Summary/excerpt of the selected source")
    selected_url: str = Field(default="", description="URL of the selected source")
    selected_image_url: str | None = Field(default=None, description="Image URL from selected source")
    # All sources for supporting context
    all_sources: list[dict] = Field(default_factory=list, description="All sources from search phase")


class ManualReplyRequest(BaseModel):
    post_url: str = Field(default="", description="URL of the X post to reply to")
    post_text: str = Field(min_length=1, max_length=2000, description="Text of the post")
    conversation_context: str = Field(default="", max_length=4000, description="Additional context or thread messages")
    reply_style: str = Field(default="analysis", description="analysis, context, punchy_take")


class ToggleSettingsRequest(BaseModel):
    publishing_enabled: bool | None = None
    dry_run: bool | None = None
    emergency_pause: bool | None = None


FORMAT_INSTRUCTIONS = {
    "analysis": "An engaging, story-driven analysis of an Indian political, tech, or economic development. Hook the reader, expose the real behind-the-scenes leverage or money trail, and give a sharp takeaway.",
    "breaking_jab": "A punchy, high-impact reaction to breaking Indian news (politics, govt decrees, opposition uproar, crazy startup drama). Highlight the drama and irony.",
    "context": "Essential narrative context behind an Indian dispute or crisis—what led here, the hidden political calculations, and who really benefits.",
    "opinion": "A bold, well-reasoned stance on an Indian policy battle, political showdown, or tech debate with zero fence-sitting.",
    "hot_take": "A contrarian, spicy observation challenging conventional Indian political or tech groupthink.",
    "relatable": "A grounded observation on real-world Indian realities (taxation, babus, urban infra, startup culture) that people instantly identify with.",
    "news_jab": "A witty, observant micro-story highlighting the irony in recent Indian political or corporate maneuvers.",
    "explainer": "A fascinating, step-by-step breakdown of how an Indian policy move, tech infrastructure (UPI/DPI/ISRO), or political strategy actually works.",
}


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/dashboard/stats")
async def get_dashboard_stats(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    now = datetime.now(UTC)

    total_sources = await db.scalar(
        select(func.count(SourceDocument.id)).where(SourceDocument.account_id == account.id)
    ) or 0
    pending_drafts = await db.scalar(
        select(func.count(Draft.id)).where(
            Draft.account_id == account.id,
            Draft.status.in_([DRAFT_READY, DRAFT_APPROVED]),
        )
    ) or 0
    pending_posts = await db.scalar(
        select(func.count(Draft.id)).where(
            Draft.account_id == account.id,
            Draft.kind == "original",
            Draft.status.in_([DRAFT_READY, DRAFT_APPROVED]),
        )
    ) or 0
    reply_suggestions = await db.scalar(
        select(func.count(Draft.id)).where(
            Draft.account_id == account.id,
            Draft.kind == "reply",
            Draft.status.in_([DRAFT_READY, DRAFT_APPROVED]),
        )
    ) or 0
    threads_count = await db.scalar(
        select(func.count(Draft.id)).where(
            Draft.account_id == account.id,
            Draft.thread_parts.is_not(None),
            func.jsonb_array_length(Draft.thread_parts) > 0,
        )
    ) or 0
    research_24h = await db.scalar(
        select(func.count(SourceDocument.id)).where(
            SourceDocument.account_id == account.id,
            SourceDocument.retrieved_at >= now - timedelta(days=1),
        )
    ) or 0
    latest_source_time = await db.scalar(
        select(func.max(SourceDocument.retrieved_at)).where(
            SourceDocument.account_id == account.id
        )
    )
    posts_today = await PostRepository.count_published_since(
        db, account.id, now - timedelta(days=1), kind="original"
    )
    total_published = await db.scalar(
        select(func.count(Post.id)).where(Post.account_id == account.id)
    ) or 0

    gemini_limits = {
        "requests_per_minute": settings.llm_requests_per_minute,
        "tokens_per_minute": settings.llm_tokens_per_minute,
        "requests_per_day": settings.llm_requests_per_day,
    }

    return {
        "account": {
            "handle": account.handle,
            "id": account.id,
            "x_user_id": account.x_user_id,
        },
        "settings": {
            "zero_cost_mode": settings.is_zero_cost_mode,
            "publishing_mode": settings.x_publishing_mode,
            "publishing_provider": "brave_cdp" if settings.x_publishing_mode == "brave_cdp" else ("local_draft" if settings.is_zero_cost_mode else settings.x_publishing_mode),
            "publishing_provider_name": "Brave Browser CDP (Automated ₹0)" if settings.x_publishing_mode == "brave_cdp" else ("Local Review Queue" if settings.is_zero_cost_mode else "X Official API"),
            "publishing_enabled": settings.publishing_enabled,
            "dry_run": settings.dry_run,
            "emergency_pause": settings.emergency_pause,
            "max_original_posts_per_day": settings.max_original_posts_per_day,
            "max_replies_per_day": settings.max_replies_per_day,
            "min_hours_between_posts": settings.min_hours_between_posts,
            "model": settings.gemini_model,
            "api_expenditure_usd": settings.api_expenditure_usd,
            "api_expenditure_inr": round(settings.api_expenditure_usd * 86.0, 2),
        },
        "stats": {
            "total_sources": total_sources,
            "pending_drafts": pending_drafts,
            "pending_posts": pending_posts,
            "scheduled_drafts": pending_drafts,
            "reply_suggestions": reply_suggestions,
            "generated_threads": threads_count,
            "research_activity_24h": research_24h,
            "last_ingestion_time": latest_source_time.isoformat() if latest_source_time else None,
            "posts_today": posts_today,
            "total_published": total_published,
        },
        "gemini_rate_limits": gemini_limits,
        "agent_health": "healthy",
        "status": "idle" if settings.emergency_pause else "monitoring_and_doomscrolling",
    }


@router.post("/api/dashboard/settings")
async def update_dashboard_settings(
    _: AdminAuth,
    settings: SettingsDep,
    payload: ToggleSettingsRequest,
) -> dict[str, Any]:
    if payload.publishing_enabled is not None:
        object.__setattr__(settings, "publishing_enabled", payload.publishing_enabled)
    if payload.dry_run is not None:
        object.__setattr__(settings, "dry_run", payload.dry_run)
    if payload.emergency_pause is not None:
        object.__setattr__(settings, "emergency_pause", payload.emergency_pause)

    return {
        "publishing_enabled": settings.publishing_enabled,
        "dry_run": settings.dry_run,
        "emergency_pause": settings.emergency_pause,
    }


@router.get("/api/dashboard/posts")
async def list_recent_posts(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    posts = list(
        (
            await db.scalars(
                select(Post)
                .where(Post.account_id == account.id)
                .order_by(Post.published_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return {
        "items": [
            {
                "id": p.id,
                "x_post_id": p.x_post_id,
                "text": p.text,
                "url": p.url or f"https://x.com/{account.handle}/status/{p.x_post_id}",
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "kind": p.kind,
            }
            for p in posts
        ]
    }


@router.get("/api/dashboard/trending")
async def list_trending_sources(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=15, ge=1, le=50),
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    banned_markers = ["britannica", "wikipedia", "inc.in", "operator.topic"]
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    
    query = (
        select(SourceDocument)
        .where(
            SourceDocument.account_id == account.id,
            SourceDocument.fetch_status != "failed",
            SourceDocument.retrieved_at >= cutoff,
        )
    )
    for marker in banned_markers:
        query = query.where(~SourceDocument.url.contains(marker))
        query = query.where(~SourceDocument.title.contains(marker))
        
    query = query.order_by(
        SourceDocument.retrieved_at.desc(),
        SourceDocument.candidate_score.desc().nullslast(),
    ).limit(limit)
    
    docs = list((await db.scalars(query)).all())
    
    # Fallback to 72h or any recent rss if no fresh documents within 24h
    if not docs:
        fallback_query = (
            select(SourceDocument)
            .where(
                SourceDocument.account_id == account.id,
                SourceDocument.fetch_status != "failed",
                SourceDocument.source_type == "rss",
            )
            .order_by(SourceDocument.retrieved_at.desc(), SourceDocument.candidate_score.desc().nullslast())
            .limit(limit)
        )
        docs = list((await db.scalars(fallback_query)).all())
    return {
        "items": [
            {
                "id": doc.id,
                "title": doc.title,
                "url": doc.url,
                "excerpt": doc.excerpt[:200],
                "score": round(doc.candidate_score or 0.0, 2),
                "source_type": doc.source_type,
                "image_url": (doc.metadata_ or {}).get("image_url"),
                "retrieved_at": doc.retrieved_at.isoformat() if doc.retrieved_at else None,
            }
            for doc in docs
        ]
    }


async def fast_refresh_rss(settings: Settings, account_id: int, db: AsyncSession) -> dict[str, int]:
    """Blazing-fast concurrent RSS feed fetcher and scorer for on-demand dashboard refresh (< 3s)."""
    headers = {"User-Agent": settings.http_user_agent}
    fetched_count = 0
    skipped_count = 0
    failed_count = 0

    async with httpx.AsyncClient(headers=headers, timeout=5.0, follow_redirects=True, verify=True) as client:
        async def fetch_one(url: str) -> tuple[str, str | None, Exception | None]:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return url, resp.text, None
                logger.warning(
                    "RSS feed returned non-200 status code",
                    extra={"extra_fields": {"feed_url": url, "status_code": resp.status_code}},
                )
                return url, None, None
            except Exception as exc:
                logger.warning(
                    "failed to fetch RSS feed in fast refresh",
                    extra={"extra_fields": {"feed_url": url, "error": str(exc)}},
                )
                return url, None, exc

        tasks = [fetch_one(u) for u in settings.feed_urls]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    items: list[IngestedItem] = []
    for resp in responses:
        if isinstance(resp, BaseException) or not isinstance(resp, tuple):
            logger.warning("Unhandled task exception fetching RSS feed", extra={"extra_fields": {"error": str(resp)}})
            failed_count += 1
            continue
        feed_url, text, err = resp
        if err is not None:
            failed_count += 1
            continue
        if not text:
            failed_count += 1
            continue
        try:
            entries = parse_feed(text)
        except Exception as exc:
            logger.warning(
                "failed to parse RSS feed xml",
                extra={"extra_fields": {"feed_url": feed_url, "error": str(exc)}},
            )
            failed_count += 1
            continue

        for entry in entries:
            fetched_count += 1
            raw_link = entry.get("link") or ""
            if not raw_link:
                links = entry.get("links") or []
                raw_link = links[0].get("href", "") if links else ""
            if not raw_link:
                skipped_count += 1
                continue
            try:
                url = normalize_url(raw_link)
                validate_url_shape(url)
            except Exception as exc:
                logger.debug(
                    "invalid url in RSS entry",
                    extra={"extra_fields": {"feed_url": feed_url, "raw_link": raw_link, "error": str(exc)}},
                )
                skipped_count += 1
                continue

            title = str(entry.get("title", "") or "").strip()
            if not title:
                skipped_count += 1
                continue

            try:
                raw_html = _entry_html(entry)
                summary_text, _ = extract_text(raw_html)
                published = _entry_datetime(entry) or datetime.now(UTC)
                profile = classify_source("rss", url)

                img_url = _entry_image(entry)
                meta = {"feed_url": feed_url, "is_primary_source": profile.is_primary}
                if img_url:
                    meta["image_url"] = img_url

                item = IngestedItem(
                    source_type="rss",
                    source_identifier=feed_url,
                    url=url,
                    title=title,
                    content=summary_text or title,
                    excerpt=clean_excerpt(summary_text or title, 280),
                    author=str(entry.get("author") or "") or None,
                    published_at=published,
                    reliability_score=profile.reliability,
                    metadata=meta,
                )
                items.append(item)
            except Exception as exc:
                logger.warning(
                    "error extracting RSS entry",
                    extra={"extra_fields": {"feed_url": feed_url, "raw_link": raw_link, "error": str(exc)}},
                )
                failed_count += 1
                continue

    stored_docs: list[SourceDocument] = []
    persona = await load_persona(db, account_id)
    scorer = CandidateScorer(settings)
    for item in items:
        try:
            doc, created = await SourceRepository.store_item(db, account_id, item)
            if created and doc is not None:
                stored_docs.append(doc)
            elif not created:
                skipped_count += 1
        except Exception as exc:
            logger.warning(
                "error storing RSS item in database",
                extra={"extra_fields": {"url": item.url, "error": str(exc)}},
            )
            failed_count += 1

    cutoff = datetime.now(UTC) - timedelta(hours=settings.freshness_window_hours)
    unscored = await SourceRepository.list_unscored(db, account_id, cutoff=cutoff, limit=50)
    all_to_score = list({d.id: d for d in stored_docs + unscored}.values())
    now = datetime.now(UTC)
    for doc in all_to_score:
        score = scorer.score_document(doc, interests=persona.interests, novelty=1.0, now=now)
        await SourceRepository.apply_score(db, doc, score)

    await db.commit()
    return {
        "fetched": fetched_count,
        "stored": len(stored_docs),
        "skipped": skipped_count,
        "failed": failed_count,
        "scored": len(all_to_score),
    }


@router.post("/api/dashboard/news/refresh")
async def refresh_news_wire(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Trigger a fast live RSS fetch from Indian news feeds, score candidates, and return fresh wire items."""
    account = await resolve_account(db, settings.x_persona_handle)
    stats = await fast_refresh_rss(settings, account.id, db)
    trending_data = await list_trending_sources(_, db, settings, limit=15)
    trending_data["stats"] = stats
    return trending_data


@router.put("/api/dashboard/drafts/{draft_id}")
async def edit_draft(
    draft_id: int,
    payload: UpdateDraftRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    draft = await db.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="draft not found")

    draft.body = payload.body.strip()
    policy = dict(draft.policy_report or {})
    if payload.image_url is not None:
        if payload.image_url.strip():
            policy["image_url"] = payload.image_url.strip()
        else:
            policy.pop("image_url", None)
        draft.policy_report = policy
    await db.commit()
    return _serialize(draft, detail=True)


@router.post("/api/dashboard/drafts/{draft_id}/refine")
async def refine_draft(
    draft_id: int,
    payload: RefineDraftRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    draft = await db.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="draft not found")

    provider = build_provider(settings)
    llm = LLMService(settings, provider, get_session_factory())
    try:
        system_prompt = (
            "You are an editor for @ssh_bugs on X.\n"
            "You rewrite draft tweets into super easy, engaging English that grabs attention.\n\n"
            "STRICT RULES:\n"
            "1. DO NOT APPEND the instruction to the original tweet. Rewrite from scratch into ONE standalone tweet (<= 220 chars).\n"
            "2. SUPER EASY ENGLISH: Write like a 10th grader. Plain, everyday words that anyone understands instantly.\n"
            "   - NO fancy or heavy words. Use punchy action words: 'busted', 'caught', 'arrested', 'lost', 'raided', 'banned', 'fined', 'ordered'.\n"
            "3. 2-PART FORMAT: Strong event or natural hook, blank line, then the key detail or consequence. Avoid overusing cheesy tags like SHOCKING: or BIG: unless specifically asked.\n"
            "4. NO HASHTAGS: Zero hashtags.\n"
            "5. STRICT LENGTH: Target 140-220 characters. Maximum 2 short sentences. Never exceed 260 characters.\n"
            "6. Output ONLY the final tweet text. Absolutely zero quotes, zero commentary, zero explanation."
        )
        user_prompt = (
            f"ORIGINAL DRAFT TWEET:\n\"\"\"{payload.current_text}\"\"\"\n\n"
            f"OPERATOR REVISION GOAL:\n\"\"\"{payload.instruction}\"\"\"\n\n"
            "Rewrite the tweet now from scratch into a single, cohesive, punchy tweet (DO NOT APPEND):"
        )
        resp = await llm.generate(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
            ],
            operation="refine_draft",
            temperature=0.7,
            max_tokens=500,
        )
        refined = resp.text.strip().strip('"').strip("'")
        if refined.startswith('"') and refined.endswith('"'):
            refined = refined[1:-1].strip()

        if not refined:
            refined = payload.current_text

        if len(refined) > 280:
            parts = refined.split(". ")
            if len(parts) > 1 and len(parts[0] + ".") <= 280:
                cand = ""
                for p in parts:
                    test_str = f"{cand}. {p}" if cand else p
                    if len(test_str) + 1 <= 280:
                        cand = test_str
                if cand:
                    refined = cand if cand.endswith(".") else cand + "."
            if len(refined) > 280:
                refined = refined[:277].rstrip() + "..."

        draft.body = refined
        await db.commit()

        return {
            "draft_id": draft_id,
            "original_text": payload.current_text,
            "refined_text": refined,
        }
    finally:
        await llm.aclose()


@router.post("/api/dashboard/refine_text")
async def refine_text(
    payload: RefineDraftRequest,
    _: AdminAuth,
    settings: SettingsDep,
) -> dict[str, Any]:
    provider = build_provider(settings)
    llm = LLMService(settings, provider, get_session_factory())
    try:
        system_prompt = (
            "You are an editor for @ssh_bugs on X.\n"
            "You rewrite draft tweets into super easy, engaging English that grabs attention.\n\n"
            "STRICT RULES:\n"
            "1. DO NOT APPEND the instruction to the original tweet. Rewrite from scratch into ONE standalone tweet (<= 220 chars).\n"
            "2. SUPER EASY ENGLISH: Write like a 10th grader. Plain, everyday words that anyone understands instantly.\n"
            "   - NO fancy or heavy words. Use punchy action words: 'busted', 'caught', 'arrested', 'lost', 'raided', 'banned', 'fined', 'ordered'.\n"
            "3. 2-PART FORMAT: Strong event or natural hook, blank line, then the key detail or consequence. Avoid overusing cheesy tags like SHOCKING: or BIG: unless specifically asked.\n"
            "4. NO HASHTAGS: Zero hashtags.\n"
            "5. STRICT LENGTH: Target 140-220 characters. Maximum 2 short sentences. Never exceed 260 characters.\n"
            "6. Output ONLY the final tweet text. Absolutely zero quotes, zero commentary, zero explanation."
        )
        user_prompt = (
            f"ORIGINAL DRAFT TWEET:\n\"\"\"{payload.current_text}\"\"\"\n\n"
            f"OPERATOR REVISION GOAL:\n\"\"\"{payload.instruction}\"\"\"\n\n"
            "Rewrite the tweet now from scratch into a single, cohesive, punchy tweet (DO NOT APPEND):"
        )
        resp = await llm.generate(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
            ],
            operation="refine_text",
            temperature=0.7,
            max_tokens=500,
        )
        refined = resp.text.strip().strip('"').strip("'")
        if refined.startswith('"') and refined.endswith('"'):
            refined = refined[1:-1].strip()

        if not refined:
            refined = payload.current_text

        if len(refined) > 280:
            parts = refined.split(". ")
            if len(parts) > 1 and len(parts[0] + ".") <= 280:
                cand = ""
                for p in parts:
                    test_str = f"{cand}. {p}" if cand else p
                    if len(test_str) + 1 <= 280:
                        cand = test_str
                if cand:
                    refined = cand if cand.endswith(".") else cand + "."
            if len(refined) > 280:
                refined = refined[:277].rstrip() + "..."

        return {
            "original_text": payload.current_text,
            "refined_text": refined,
        }
    finally:
        await llm.aclose()


@router.post("/api/dashboard/drafts/{draft_id}/mark_published")
async def mark_draft_published(
    draft_id: int,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Mark a draft as published manually by the operator.
    
    Strictly updates local database records and never touches api.x.com.
    """
    account = await resolve_account(db, settings.x_persona_handle)
    draft = await db.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="draft not found")

    now = datetime.now(UTC)
    draft.status = DRAFT_PUBLISHED
    draft.published_at = now
    draft.reviewed_at = now
    
    local_post_id = f"manual_{draft.id}_{int(now.timestamp())}"
    await PostRepository.create(
        db,
        account_id=account.id,
        x_post_id=local_post_id,
        text=draft.body,
        draft_id=draft.id,
        position=0,
        published_at=now,
        kind=draft.kind or "original",
    )
    
    await AuditRepository.log(
        db,
        action="mark_published_manual",
        outcome="success",
        account_id=account.id,
        details={
            "draft_id": draft.id,
            "kind": draft.kind,
            "local_post_id": local_post_id,
            "zero_cost": True,
        },
    )
    await db.commit()
    
    return {
        "status": "published",
        "mode": "manual_local_record",
        "draft_id": draft.id,
        "x_post_id": local_post_id,
        "published_at": now.isoformat(),
        "zero_cost": True,
    }


@router.post("/api/dashboard/replies/manual_generate")
async def manual_generate_replies(
    payload: ManualReplyRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Generate context-aware geopolitical reply suggestions for operator manual posting."""
    account = await resolve_account(db, settings.x_persona_handle)
    provider = build_provider(settings)
    session_factory = get_session_factory()
    llm = LLMService(settings, provider, session_factory)
    try:
        drafter = ReplyDrafter(settings, llm, session_factory)
        result = await drafter.generate_manual_reply(
            account_id=account.id,
            post_text=payload.post_text,
            post_url=payload.post_url,
            conversation_context=payload.conversation_context,
            preferred_style=payload.reply_style,
        )
        return result
    finally:
        await llm.aclose()


@router.post("/api/dashboard/drafts/{draft_id}/publish_now")
async def publish_draft_now(
    draft_id: int,
    _: AdminAuth,
    settings: SettingsDep,
) -> dict[str, Any]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        draft = await session.get(Draft, draft_id)
        if draft is not None:
            v = dict(draft.verification_report or {})
            v["passed"] = True
            v.setdefault("unverified_facts", 0)
            v["operator_approved"] = True
            draft.verification_report = v
            
            p = dict(draft.policy_report or {})
            p["operator_approved"] = True
            p["duplicate"] = {"duplicate": False, "method": "operator_approval"}
            draft.policy_report = p
            draft.status = DRAFT_READY
            draft.error = None
            await session.commit()

    try:
        embedder = build_embedding_provider(settings)
    except Exception:
        embedder = None
    x_client = (
        XClient(settings)
        if (settings.x_write_credentials_present and not settings.is_zero_cost_mode)
        else None
    )
    publisher = Publisher(settings, session_factory, x_client, embedder=embedder)

    result = await publisher.publish_original(draft_id)
    return {
        "status": result.status,
        "draft_id": result.draft_id,
        "post_ids": result.post_ids,
        "reasons": result.reasons,
    }


@router.post("/api/dashboard/command/draft")
async def command_draft(
    payload: CommandDraftRequest,
    admin: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    query = payload.topic.strip()
    format_name = payload.format.lower() if payload.format else "analysis"
    format_brief = FORMAT_INSTRUCTIONS.get(format_name, FORMAT_INSTRUCTIONS["analysis"])

    # Step 1: Research live context from Google News RSS
    research_context = ""
    doc: SourceDocument | None = None
    cmd_image_url: str | None = None
    encoded = quote_plus(query)
    feed_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN%3Aen"

    try:
        async with httpx.AsyncClient(timeout=8.0, verify=True) as client:
            resp = await client.get(feed_url, headers={"User-Agent": settings.http_user_agent})
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                if feed.entries:
                    top_entries = feed.entries[:3]
                    snippets = [f"- {getattr(e, 'title', '')}" for e in top_entries if getattr(e, 'title', '')]
                    research_context = "\n".join(snippets)
                    entry0 = feed.entries[0]
                    link = getattr(entry0, "link", f"https://news.google.com/search?q={encoded}")
                    title0 = getattr(entry0, "title", query)
                    summary0 = getattr(entry0, "summary", query)
                    cmd_image_url = _entry_image(entry0)
                    if not cmd_image_url and link and link.startswith("http"):
                        try:
                            resp_art = await client.get(link, follow_redirects=True, timeout=4.0)
                            if resp_art.status_code == 200:
                                cmd_image_url = extract_article_image(resp_art.text)
                        except Exception:
                            pass

                    item_meta = {"query": query}
                    if cmd_image_url:
                        item_meta["image_url"] = cmd_image_url

                    item = IngestedItem(
                        source_type="web",
                        source_identifier=link,
                        url=link,
                        title=title0,
                        content=summary0,
                        excerpt=summary0[:500],
                        reliability_score=0.85,
                        metadata=item_meta,
                    )
                    doc, _created = await SourceRepository.store_item(db, account.id, item)
                    await db.commit()
    except Exception as exc:
        logger.warning("online search for command failed, falling back to manual topic", extra={"extra_fields": {"error": str(exc)}})

    if doc is None:
        virtual_url = f"https://operator.topic/{content_hash(query)[:16]}"
        manual_item = IngestedItem(
            source_type="manual",
            source_identifier=virtual_url,
            url=virtual_url,
            title=query,
            content=f"Operator requested topic: {query}. Geopolitical affairs, international relations, regional policy perspective.",
            excerpt=query,
            reliability_score=0.9,
        )
        doc, _created = await SourceRepository.store_item(db, account.id, manual_item)
        await db.commit()

    # Step 2: Generate the tweet directly using world affairs persona
    provider = build_provider(settings)
    llm = LLMService(settings, provider, get_session_factory())
    try:
        system_prompt = (
            "You are @ssh_bugs (NotANerd) on X.\n"
            "You post fast, engaging, and super viral updates on India's craziest news, politics, tech, scams, and court battles.\n\n"
            "RULES FOR SUPER EASY ENGLISH & HIGH ENGAGEMENT:\n"
            "1. SUPER SIMPLE ENGLISH: Write like a 10th grader. Plain, everyday words that anyone scrolling on X understands instantly.\n"
            "   - NEVER use heavy or fancy words like 'extradition', 'lax verification', 'fever', 'slammed a blow', 'promulgated'.\n"
            "   - USE punchy everyday action words: 'busted', 'caught', 'arrested', 'lost', 'banned', 'raided', 'fined', 'ordered', 'tricked'.\n"
            "   - BANNED ROBOT PHRASES: NEVER say 'Analysts say', 'The real win', 'The punch?', 'While X cheers', 'dwarfs the', 'In a surprising move'.\n"
            "2. HIGH ENGAGEMENT: Make people stop scrolling. Focus on the core fact, huge number (₹ crores, counts), or crazy detail.\n"
            "3. VARY YOUR HOOKS (DO NOT ALWAYS USE UPPERCASE ALERTS):\n"
            "   - DO NOT start every tweet with 'SHOCKING:', 'BIG:', or 'BREAKING:'. Overusing them looks spammy.\n"
            "   - 80% of the time, start naturally with a direct lead sentence (e.g. 'Supreme Court just banned...', 'A Bengaluru techie lost...').\n"
            "   - Only use 'UPDATE:' or 'JUST IN:' occasionally (at most 20% of the time for major fast-moving news).\n"
            "4. 2-PART FORMAT (WITH A BLANK LINE):\n"
            "   Line 1: The big event or direct hook in one clear sentence.\n"
            "   Line 2: The wildest detail, amount of money, or consequence.\n"
            "5. STRICT LENGTH: Target 140-220 characters total! Maximum 2 short lines. Twitter hard limit is 280. Zero hashtags.\n"
            "6. OUTPUT FORMAT:\n"
            "Output EXACTLY in this format:\n"
            "TWEET: <the tweet text>\n"
            "RATIONALE: <1 sentence why this resonates>"
        )
        user_prompt = (
            f"TOPIC / EVENT:\n{query}\n\n"
            f"FORMAT STYLE: {format_name.upper()}\n"
            f"STYLE GOAL: {format_brief}\n\n"
            + (f"LATEST INTERNET CONTEXT / HEADLINES:\n{research_context}\n\n" if research_context else "")
            + "Draft the single best standalone tweet for this topic now:"
        )

        llm_resp = await llm.generate([
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ], operation="command_draft", temperature=0.75, max_tokens=220)

        raw_clean = llm_resp.text.strip()
        tweet_text = ""
        rationale = f"On-demand {format_name} command by operator"
        if "TWEET:" in raw_clean.upper():
            parts = re.split(r"(?i)\bRATIONALE:\s*", raw_clean, maxsplit=1)
            tweet_part = parts[0]
            if len(parts) > 1:
                rationale = parts[1].strip()
            match = re.search(r"(?i)\bTWEET:\s*", tweet_part)
            tweet_text = tweet_part[match.end():].strip() if match else tweet_part.strip()
        else:
            tweet_text = raw_clean

        if tweet_text.startswith('"') and tweet_text.endswith('"'):
            tweet_text = tweet_text[1:-1].strip()

        # Strict character length clamp to guarantee <= 280 characters
        if len(tweet_text) > 280:
            parts = tweet_text.split(". ")
            if len(parts) > 1 and len(parts[0] + ".") <= 280:
                candidate = ""
                for p in parts:
                    test_str = f"{candidate}. {p}" if candidate else p
                    if len(test_str) + 1 <= 280:
                        candidate = test_str
                if candidate:
                    tweet_text = candidate if candidate.endswith(".") else candidate + "."
            if len(tweet_text) > 280:
                tweet_text = tweet_text[:277].rstrip() + "..."

        # Step 3: Persist Draft in database with status READY
        key = f"cmd:{account.id}:{content_hash(query)[:12]}:{int(datetime.now(UTC).timestamp())}"
        cmd_policy = {
            "source": "operator_command",
            "format": format_name,
            "operator_approved": True,
            "duplicate": {"duplicate": False, "method": "operator_command"},
        }
        if cmd_image_url:
            cmd_policy["image_url"] = cmd_image_url

        draft = await DraftRepository.create(
            db,
            account_id=account.id,
            kind="original",
            status=DRAFT_READY,
            idempotency_key=key,
            topic=query[:500],
            title=query[:500],
            body=tweet_text,
            thread_parts=[],
            tone=format_name,
            rationale=rationale,
            source_document_ids=[doc.id] if doc else [],
            candidate_score=0.9,
            verification_report={"passed": True, "claims": [], "unverified_facts": 0, "operator_command": True},
            policy_report=cmd_policy,
        )
        await db.commit()
        await db.refresh(draft)

        # Step 4: Publish immediately if requested
        published_info = None
        if payload.publish_now:
            try:
                embedder = build_embedding_provider(settings)
            except Exception:
                embedder = None
            x_client = (
        XClient(settings)
        if (settings.x_write_credentials_present and not settings.is_zero_cost_mode)
        else None
    )
            publisher = Publisher(settings, get_session_factory(), x_client, embedder=embedder)
            pub_result = await publisher.publish_original(draft.id)
            published_info = {
                "status": pub_result.status,
                "post_ids": pub_result.post_ids,
                "reasons": pub_result.reasons,
            }

        return {
            "draft": _serialize(draft, detail=True),
            "status": "ready",
            "published": published_info,
        }
    finally:
        await llm.aclose()


# ---------------------------------------------------------------------------
# Dashboard HTML Template
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Deep Research & On-Demand News Crawl
# ---------------------------------------------------------------------------

async def _deep_research_crawl(
    query: str, settings: Settings
) -> list[dict[str, Any]]:
    """Phase 1: Concurrent multi-source research crawl. Returns ranked list of fresh {title,summary,url,time_ago} (<=24h max)."""
    encoded = quote_plus(query)
    SEARCH_FEEDS = [
        f"https://news.google.com/rss/search?q={encoded}+when:1d&hl=en-IN&gl=IN&ceid=IN%3Aen",
        f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN%3Aen",
        "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN%3Aen",
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "https://feeds.feedburner.com/ndtvnews-india-news",
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "https://indianexpress.com/section/india/feed/",
    ]
    now = datetime.now(UTC)

    async def _fetch_feed(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        try:
            r = await client.get(url, timeout=5.0)
            if r.status_code != 200:
                return []
            feed = feedparser.parse(r.text)
            results = []
            for entry in feed.entries[:25]:
                title = str(entry.get("title", "") or "").strip()
                summary = str(entry.get("summary", "") or entry.get("description", "") or "").strip()
                link = str(entry.get("link", "") or "").strip()
                if not title or not link:
                    continue

                pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                age_hours = 12.0
                time_ago = "Recent (< 24h)"

                if pub_parsed:
                    try:
                        pub_dt = datetime.fromtimestamp(calendar.timegm(pub_parsed), tz=UTC)
                        age_sec = (now - pub_dt).total_seconds()
                        if age_sec < 0:
                            age_sec = 0
                        age_hours = age_sec / 3600.0
                        # STRICT 1 DAY MAX FILTER (24h)
                        if age_hours > 24.0:
                            continue
                        if age_hours < 1.0:
                            mins = max(1, int(age_sec / 60))
                            time_ago = f"{mins}m ago"
                        else:
                            time_ago = f"{int(age_hours)}h ago"
                    except Exception:
                        pass

                img_url = _entry_image(entry)
                results.append({
                    "title": title,
                    "summary": summary[:400],
                    "url": link,
                    "image_url": img_url,
                    "time_ago": time_ago,
                    "age_hours": age_hours,
                })
            return results
        except Exception:
            return []

    headers = {"User-Agent": settings.http_user_agent}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, verify=True) as client:
        feed_results = await asyncio.gather(*[_fetch_feed(client, u) for u in SEARCH_FEEDS], return_exceptions=True)

    all_items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    query_lower = query.lower()
    query_words = set(w for w in query_lower.split() if len(w) > 3)

    for r in feed_results:
        if isinstance(r, BaseException):
            continue
        for item in r:
            key = item["title"].lower()[:60]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            all_items.append(item)

    spicy_terms = {
        "slams", "hits back", "sparks row", "backlash", "spat", "furious", "outrage",
        "accuses", "clash", "faceoff", "mock", "mocks", "dig at", "shuts down",
        "unfiltered", "revolt", "boycott", "allegations", "warns", "defiant",
        "heckled", "hypocrisy", "uproar", "controversy", "blasts", "rips into",
        "bizarre", "absurd", "ironic", "unbelievable", "caught", "scam", "digital arrest",
        "conman", "busted", "fake", "raided", "cash", "blunder", "goof up", "hilarious",
        "banned", "illegal", "viral", "stunt", "extortion", "caught red handed",
        "charging port", "train delayed", "refund", "rent", "70 hour", "work culture",
        "tax notice", "challan", "fine", "consumer court", "betting app", "ed summons",
    }
    boring_terms = {
        "annual report", "panel discussion", "webinar", "seminar", "workshop", "proceedings adjourned",
        "inauguration", "foundation stone", "held meeting", "delegation arrives", "bilateral talks",
        "courtesy visit", "reviews preparedness", "mou signed", "routine transfer", "obituary",
        "weather forecast", "horoscope",
    }

    def rank_key(it: dict[str, Any]) -> tuple[float, float, float]:
        text = (it["title"] + " " + it["summary"]).lower()
        match_count = sum(1 for w in query_words if w in text)
        recency = max(0.0, 24.0 - float(it.get("age_hours", 12.0)))
        spicy_score = sum(1.5 for term in spicy_terms if term in text)
        if any(term in it["title"].lower() for term in spicy_terms):
            spicy_score += 3.0
        if any(b in text for b in boring_terms):
            spicy_score -= 4.0
        return (float(match_count), spicy_score, recency)

    all_items.sort(key=rank_key, reverse=True)
    return all_items[:10]


@router.post("/api/dashboard/deep_research/sources")
async def deep_research_sources(
    payload: DeepResearchRequest,
    _: AdminAuth,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Phase 1 only: crawl live sources and return them for operator to pick from."""
    sources = await _deep_research_crawl(payload.topic.strip(), settings)
    return {"sources": sources, "query": payload.topic.strip()}


@router.post("/api/dashboard/deep_research/draft")
async def deep_research_draft(
    payload: DeepResearchDraftRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Phase 2: draft a tweet focused on the operator-selected source, using all sources as supporting context."""
    account = await resolve_account(db, settings.x_persona_handle)
    query = payload.topic.strip()
    format_name = payload.format.lower() if payload.format else "analysis"
    format_brief = FORMAT_INSTRUCTIONS.get(format_name, FORMAT_INSTRUCTIONS["analysis"])

    selected_title = payload.selected_title.strip()
    selected_summary = payload.selected_summary.strip()
    selected_url = payload.selected_url.strip()
    selected_image_url = payload.selected_image_url.strip() if payload.selected_image_url else None
    all_sources = payload.all_sources or []

    # If no editorial image URL provided, attempt to fetch authentic og:image directly from article
    if not selected_image_url and selected_url and selected_url.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True, verify=True) as client:
                art_resp = await client.get(selected_url, headers={"User-Agent": settings.http_user_agent})
                if art_resp.status_code == 200:
                    cand_img = extract_article_image(art_resp.text)
                    if cand_img:
                        selected_image_url = cand_img
        except Exception:
            pass

    # Build context: selected source goes FIRST and prominently, then supporting sources
    research_lines: list[str] = []
    if selected_title:
        research_lines.append("FOCUS STORY (operator selected this):")
        research_lines.append(f"  Title: {selected_title}")
        if selected_summary:
            research_lines.append(f"  Context: {selected_summary[:400]}")
        research_lines.append("")
        research_lines.append("SUPPORTING CONTEXT (other live sources):")
        for i, s in enumerate(all_sources[:5], 1):
            if s.get("title", "") != selected_title:
                research_lines.append(f"  {i}. {s['title']}")
                if s.get("summary"):
                    research_lines.append(f"     → {s['summary'][:200]}")
    else:
        for i, s in enumerate(all_sources[:6], 1):
            research_lines.append(f"  {i}. {s.get('title', '')}")
            if s.get("summary"):
                research_lines.append(f"     → {s['summary'][:200]}")

    research_context = "\n".join(research_lines)

    # Ingest / store source document if url provided
    doc = None
    if selected_url and selected_title:
        item = IngestedItem(
            source_type="web",
            source_identifier=selected_url,
            url=selected_url,
            title=selected_title,
            content=f"{selected_title}\n\n{selected_summary}",
            excerpt=selected_summary[:500],
            reliability_score=0.85,
            metadata={"image_url": selected_image_url} if selected_image_url else {},
        )
        doc, _created = await SourceRepository.store_item(db, account.id, item)
        await db.commit()
    elif all_sources:
        top_s = all_sources[0]
        t_url = top_s.get("url")
        if t_url:
            item = IngestedItem(
                source_type="web",
                source_identifier=t_url,
                url=t_url,
                title=top_s.get("title", query),
                content=f"{top_s.get('title', query)}\n\n{top_s.get('summary', '')}",
                excerpt=top_s.get("summary", "")[:500],
                reliability_score=0.85,
            )
            doc, _created = await SourceRepository.store_item(db, account.id, item)
            await db.commit()

    if doc is None:
        virtual_url = f"https://operator.research/{content_hash(query)[:16]}"
        manual_item = IngestedItem(
            source_type="manual",
            source_identifier=virtual_url,
            url=virtual_url,
            title=selected_title or query,
            content=f"Deep research request: {query}",
            excerpt=query,
            reliability_score=0.9,
        )
        doc, _created = await SourceRepository.store_item(db, account.id, manual_item)
        await db.commit()

    # Generate the tweet
    provider = build_provider(settings)
    llm = LLMService(settings, provider, get_session_factory())
    try:
        system_prompt = (
            "You are @ssh_bugs (NotANerd) on X.\n"
            "You write sharp, viral, scroll-stopping posts on India's most controversial news, heated political statements, wild clashes, bizarre scams, and relatable outrage.\n\n"
            "RULES FOR MAXIMUM ENGAGEMENT & HIGH INTEREST ON X:\n"
            "1. ATTENTION-GRABBING HOOK (Line 1): Grab the reader immediately. Never start like a boring news agency.\n"
            "   - If it is a spicy political clash or controversial quote: Lead directly with the sharpest punchline or dig.\n"
            "     e.g. \"Sambit Patra did not hold back at the INDIA bloc rally: 'You claimed 10 lakh turnout, but 40% of Ramlila Maidan was empty.'\"\n"
            "     e.g. \"Amit Shah fired back at Rahul Gandhi in Parliament: 'If you want to act like PM, stay in mock parliament.'\"\n"
            "   - If it is an absurd, funny, or crazy real-life event / scam: Spotlight the irony or absurdity right away.\n"
            "     e.g. \"Indian Railways just banned phone charging on trains from 11 PM to 5 AM to prevent fire hazards.\"\n"
            "     e.g. \"A fake CBI officer ran an entire office from an apartment and extorted ₹3 Crore before getting busted.\"\n"
            "   - NEVER start with 'In a development...', 'The government has...', 'According to sources...', or 'SHOCKING:'.\n"
            "2. THE TWIST OR DETAIL (Line 2): Follow up with the wild number (₹ crores), hilarious consequence, or punchline that makes people quote-tweet.\n"
            "3. SUPER SIMPLE ENGLISH: Write in plain everyday English that anyone on mobile understands instantly.\n"
            "4. 2-PART FORMAT (WITH A BLANK LINE):\n"
            "   Line 1: Hook / Clash / Outrageous statement.\n"
            "   (blank line)\n"
            "   Line 2: Wild detail, consequence, or witty observation.\n"
            "5. STRICT LENGTH: 140-220 characters total! Max 2 short lines. No hashtags. Zero emojis.\n"
            "6. OUTPUT EXACTLY:\n"
            "TWEET: <tweet text>\n"
            "RATIONALE: <1 sentence why this grabs attention>"
        )
        focus_label = f'"{selected_title}"' if selected_title else query
        user_prompt = (
            f"TOPIC HINT FROM OPERATOR: {query}\n"
            f"OPERATOR SELECTED THIS STORY TO DRAFT FROM: {focus_label}\n\n"
            f"FORMAT STYLE: {format_name.upper()}\n"
            f"STYLE GOAL: {format_brief}\n\n"
            f"RESEARCH CONTEXT:\n{research_context}\n\n"
            "Draft the high-engagement tweet now, focused on the FOCUS STORY:"
        )

        resp = await llm.generate(
            [LLMMessage(role="system", content=system_prompt),
             LLMMessage(role="user", content=user_prompt)],
            operation="deep_research_draft",
            temperature=0.72,
            max_tokens=250,
        )

        raw_clean = resp.text.strip()
        tweet_text = ""
        rationale = f"Deep research {format_name}: {selected_title or query}"
        if "TWEET:" in raw_clean.upper():
            parts_r = re.split(r"(?i)\bRATIONALE:\s*", raw_clean, maxsplit=1)
            tweet_part = parts_r[0]
            if len(parts_r) > 1:
                rationale = parts_r[1].strip()
            match = re.search(r"(?i)\bTWEET:\s*", tweet_part)
            tweet_text = tweet_part[match.end():].strip() if match else tweet_part.strip()
        else:
            tweet_text = raw_clean

        if tweet_text.startswith('"') and tweet_text.endswith('"'):
            tweet_text = tweet_text[1:-1].strip()
        tweet_text = re.sub(r"\n{3,}", "\n\n", tweet_text)

        if not tweet_text:
            tweet_text = raw_clean

        if doc is not None:
            doc.processed_at = datetime.now(UTC)
            await db.commit()

        research_policy = {
            "source": "deep_research",
            "format": format_name,
            "operator_approved": True,
            "duplicate": {"duplicate": False, "method": "deep_research"},
        }
        if selected_image_url:
            research_policy["image_url"] = selected_image_url

        key = f"research:{account.id}:{content_hash(query + selected_title)[:12]}:{int(datetime.now(UTC).timestamp())}"
        draft = await DraftRepository.create(
            db,
            account_id=account.id,
            kind="original",
            status=DRAFT_READY,
            idempotency_key=key,
            topic=query[:500],
            title=(selected_title or query)[:500],
            body=tweet_text,
            thread_parts=[],
            tone=format_name,
            rationale=rationale,
            source_document_ids=[doc.id] if doc else [],
            candidate_score=0.9,
            verification_report={"passed": True, "claims": [], "unverified_facts": 0, "deep_research": True},
            policy_report=research_policy,
        )
        await db.commit()
        await db.refresh(draft)

        return {
            "draft": _serialize(draft, detail=True),
            "selected_source": {"title": selected_title, "url": selected_url, "summary": selected_summary},
        }
    finally:
        await llm.aclose()


# ---------------------------------------------------------------------------
# Dashboard Template Loader & Route
# ---------------------------------------------------------------------------

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


@router.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard(settings: SettingsDep) -> str:
    raw_html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    token = settings.admin_api_token.get_secret_value() if settings.admin_api_token else ""
    if token:
        raw_html = raw_html.replace("{{ ADMIN_TOKEN }}", token)
        raw_html = raw_html.replace('id="adminToken" placeholder="Paste ADMIN_API_TOKEN from .env"', f'id="adminToken" value="{token}"')
    return raw_html


@router.get("/", response_class=HTMLResponse)
async def root_redirect(settings: SettingsDep) -> str:
    return await serve_dashboard(settings)
