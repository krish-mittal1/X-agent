import sys
sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import json
from datetime import UTC, datetime

from app.config import get_settings
from app.memory.database import get_session_factory, init_engine, session_scope
from app.memory.models import (
    Draft,
    EditorialEvaluation,
    Event,
    EventDevelopment,
    EventPostLink,
    ResearchReport,
    ResearchSource,
    SourceDocument,
)
from sqlalchemy import select, text

async def extract():
    settings = get_settings()
    init_engine(settings)
    session_factory = get_session_factory()

    async with session_factory() as session:
        drafts = (await session.scalars(
            select(Draft).where(Draft.id >= 83).order_by(Draft.id.asc())
        )).all()

        pilot_data = []
        for d in drafts:
            policy = d.policy_report or {}
            verif = d.verification_report or {}
            
            # Fetch evaluation
            ev = (await session.scalars(
                select(EditorialEvaluation).where(EditorialEvaluation.draft_id == d.id)
            )).first()

            # Fetch event post link
            link = (await session.scalars(
                select(EventPostLink).where(EventPostLink.draft_id == d.id)
            )).first()
            event = await session.get(Event, link.event_id) if link else None

            # Fetch research report
            rep_id = policy.get("research_report_id")
            rep = await session.get(ResearchReport, rep_id) if rep_id else None
            
            sources_count = 0
            if rep:
                sources_count = len((await session.scalars(
                    select(ResearchSource).where(ResearchSource.report_id == rep.id)
                )).all())

            # Original document if linked
            source_doc_id = d.source_document_ids[0] if d.source_document_ids else None
            source_doc = await session.get(SourceDocument, source_doc_id) if source_doc_id else None

            item = {
                "draft_id": d.id,
                "status": d.status,
                "topic": d.topic,
                "selected_angle": d.tone,
                "body": d.body,
                "body_length": len(d.body) if d.body else 0,
                "rationale": d.rationale,
                "source_doc_id": source_doc_id,
                "source_title": source_doc.title if source_doc else d.title,
                "source_url": source_doc.url if source_doc else None,
                "source_type": source_doc.source_type if source_doc else None,
                "source_reliability": source_doc.reliability_score if source_doc else None,
                "research": {
                    "report_id": rep.id if rep else None,
                    "confidence_score": rep.confidence_score if rep else None,
                    "sources_count": sources_count,
                    "conflicts_count": len(rep.conflicting_info) if (rep and rep.conflicting_info) else 0,
                },
                "event": {
                    "event_id": event.id if event else None,
                    "event_title": event.title if event else None,
                    "event_status": event.status if event else None,
                    "linked_angle": link.context_angle if link else None,
                },
                "critic": {
                    "overall_score": ev.overall_score if ev else None,
                    "verdict": ev.verdict if ev else None,
                    "critic_reasoning": ev.critic_reasoning if ev else None,
                    "insight_score": ev.insight_score if ev else None,
                    "persona_score": ev.persona_score if ev else None,
                } if ev else None,
                "verification": {
                    "passed": verif.get("passed", False),
                    "verified_facts": verif.get("verified_facts", 0),
                    "unverified_facts": verif.get("unverified_facts", 0),
                    "contradicted_claims": verif.get("contradicted_claims", 0),
                    "hard_stop_triggered": verif.get("hard_stop_triggered", False),
                    "hard_stop_reasons": verif.get("hard_stop_reasons", []),
                    "issues": verif.get("issues", []),
                },
                "alternatives": [
                    {"angle": a.get("angle"), "body": a.get("parts", [""])[0]}
                    for a in policy.get("alternative_angles", [])
                ],
                "blocking_reasons": policy.get("blocking_reasons", []),
            }
            pilot_data.append(item)

    with open("pilot_extracted_data.json", "w", encoding="utf-8") as f:
        json.dump(pilot_data, f, indent=2, default=str)

    print(f"Successfully extracted data for {len(pilot_data)} pilot drafts to 'pilot_extracted_data.json'")

if __name__ == "__main__":
    asyncio.run(extract())
