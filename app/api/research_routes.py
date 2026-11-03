"""API routes for Multi-Source Research Engine and Event Intelligence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from app.api.deps import AdminAuth, DbSession, SettingsDep
from app.api.drafts import resolve_account
from app.memory.models import Event, ResearchReport
from app.research.event_service import EventIntelligenceService
from app.research.schemas import ResearchSourceRequest
from app.research.service import ResearchService

router = APIRouter(prefix="/api/research", tags=["research"])


@router.post("/investigate")
async def investigate_topic(
    payload: ResearchSourceRequest,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Execute multi-source investigation and synthesize a structured research report."""
    account = await resolve_account(db, settings.x_persona_handle)
    service = ResearchService(settings)
    try:
        report = await service.conduct_research(
            db, account.id, payload.query.strip(), max_sources=payload.max_sources
        )
    finally:
        await service.aclose()

    # Match or update event timeline
    event_service = EventIntelligenceService(settings)
    event, is_new, delta_note = await event_service.match_or_create_event(db, account.id, report)

    return {
        "report_id": report.id,
        "topic": report.topic,
        "executive_summary": report.executive_summary,
        "what_happened": report.what_happened,
        "key_entities": report.key_entities,
        "verified_facts": report.verified_facts,
        "unresolved_claims": report.unresolved_claims,
        "conflicting_info": report.conflicting_info,
        "confidence_score": report.confidence_score,
        "event": {
            "id": event.id,
            "title": event.title,
            "is_new": is_new,
            "delta_analysis": delta_note,
        },
    }


@router.get("/reports")
async def list_research_reports(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    account = await resolve_account(db, settings.x_persona_handle)
    query = (
        select(ResearchReport)
        .where(ResearchReport.account_id == account.id)
        .order_by(desc(ResearchReport.id))
        .limit(limit)
    )
    reports = (await db.scalars(query)).all()
    return [
        {
            "id": r.id,
            "topic": r.topic,
            "executive_summary": r.executive_summary[:200],
            "confidence_score": r.confidence_score,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reports
    ]


@router.get("/reports/{report_id}")
async def get_research_report(
    report_id: int,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    account = await resolve_account(db, settings.x_persona_handle)
    query = (
        select(ResearchReport)
        .options(selectinload(ResearchReport.sources), selectinload(ResearchReport.claims))
        .where(ResearchReport.id == report_id, ResearchReport.account_id == account.id)
    )
    report = await db.scalar(query)
    if report is None:
        raise HTTPException(status_code=404, detail="Research report not found")

    return {
        "id": report.id,
        "topic": report.topic,
        "executive_summary": report.executive_summary,
        "what_happened": report.what_happened,
        "key_entities": report.key_entities,
        "verified_facts": report.verified_facts,
        "unresolved_claims": report.unresolved_claims,
        "conflicting_info": report.conflicting_info,
        "historical_context": report.historical_context,
        "stakeholder_analysis": report.stakeholder_analysis,
        "potential_implications": report.potential_implications,
        "confidence_score": report.confidence_score,
        "sources": [
            {
                "id": s.id,
                "url": s.url,
                "title": s.title,
                "publisher": s.publisher,
                "source_type": s.source_type,
                "is_primary": s.is_primary,
                "reliability_score": s.reliability_score,
                "content_excerpt": s.content_excerpt[:300],
            }
            for s in report.sources
        ],
        "claims": [
            {
                "id": c.id,
                "claim_text": c.claim_text,
                "claim_type": c.claim_type,
                "confidence": c.confidence,
                "is_disputed": c.is_disputed,
                "context_notes": c.context_notes,
            }
            for c in report.claims
        ],
    }


@router.get("/events")
async def list_events(
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
    status_filter: str = Query(default="active"),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    account = await resolve_account(db, settings.x_persona_handle)
    query = (
        select(Event)
        .options(selectinload(Event.entities), selectinload(Event.developments))
        .where(Event.account_id == account.id)
        .order_by(desc(Event.latest_timestamp))
        .limit(limit)
    )
    events = (await db.scalars(query)).all()
    return [
        {
            "id": e.id,
            "title": e.title,
            "summary": e.summary[:250],
            "category": e.category,
            "status": e.status,
            "initial_timestamp": e.initial_timestamp.isoformat() if e.initial_timestamp else None,
            "latest_timestamp": e.latest_timestamp.isoformat() if e.latest_timestamp else None,
            "developments_count": len(e.developments),
            "entities": [ent.name for ent in e.entities[:5]],
        }
        for e in events
    ]


@router.get("/events/{event_id}/timeline")
async def get_event_timeline(
    event_id: int,
    _: AdminAuth,
    db: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    event_service = EventIntelligenceService(settings)
    account = await resolve_account(db, settings.x_persona_handle)
    mem_context = await event_service.retrieve_event_memory_context(db, account.id, event_id)
    if not mem_context:
        raise HTTPException(status_code=404, detail="Event not found")
    return mem_context

