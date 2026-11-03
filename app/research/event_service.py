"""Event Intelligence & Long-Term Timeline Memory Service.

Tracks unfolding Indian news stories over time, detects new developments,
maintains chronological timelines, and prevents repetitive commentary by supplying
past coverage context to the generation engine.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider, build_embedding_provider
from app.memory.models import (
    EditorialMemory,
    Event,
    EventDevelopment,
    EventEntity,
    EventPostLink,
    Post,
    ResearchReport,
)

logger = get_logger(__name__)

_GENERIC_ENTITIES = {
    "india", "indian", "delhi", "new delhi", "bengaluru", "bangalore", "mumbai",
    "government", "central government", "state government", "ministry", "cabinet",
    "parliament", "lok sabha", "rajya sabha", "supreme court", "high court",
    "official", "spokesperson", "authorities", "police", "board", "commission",
    "centre", "union",
}

_STOP_WORDS = {
    "the", "and", "with", "from", "that", "this", "for", "its", "was", "were",
    "has", "have", "are", "will", "can", "more", "than", "into", "using", "used",
    "about", "after", "over", "under", "between", "india", "indian", "says", "said",
    "report", "reports", "reported", "first", "also", "then", "been", "what", "which",
    "their", "there", "other", "some", "such", "only", "would",
}


def _normalize_name(name: str) -> str:
    return re.sub(r"\W+", "", name.lower())


def _clean_entity_for_overlap(name: str) -> str:
    clean = re.sub(r"\W+", " ", name.lower()).strip()
    clean = re.sub(r"\b(pvt|ltd|limited|corp|corporation|inc|co)\b", "", clean).strip()
    return re.sub(r"\s+", "", clean)


def _extract_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS}


def _calculate_entity_overlap(candidate_names: set[str], ev_names: set[str]) -> float:
    cand_cleaned = {_clean_entity_for_overlap(n) for n in candidate_names}
    ev_cleaned = {_clean_entity_for_overlap(n) for n in ev_names}
    cand_filtered = {n for n in cand_cleaned if n not in _GENERIC_ENTITIES and len(n) > 2}
    ev_filtered = {n for n in ev_cleaned if n not in _GENERIC_ENTITIES and len(n) > 2}
    if not cand_filtered or not ev_filtered:
        cand_all = {n for n in cand_cleaned if len(n) > 2}
        ev_all = {n for n in ev_cleaned if len(n) > 2}
        if not cand_all or not ev_all:
            return 0.0
        inter = cand_all & ev_all
        return 0.25 * (len(inter) / len(cand_all | ev_all))
    inter = cand_filtered & ev_filtered
    if not inter:
        return 0.0
    cand_ratio = len(inter) / len(cand_filtered)
    jaccard = len(inter) / len(cand_filtered | ev_filtered)
    return 0.5 * cand_ratio + 0.5 * jaccard


def _cosine_similarity(vec1: list[float] | None, vec2: list[float] | None) -> float:
    """Compute cosine similarity. Returns 0.0 if either vector is missing, empty, or mismatched."""
    if not vec1 or not vec2 or len(vec1) != len(vec2) or len(vec1) == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2, strict=False))
    mag1 = sum(a * a for a in vec1) ** 0.5
    mag2 = sum(b * b for b in vec2) ** 0.5
    if mag1 == 0.0 or mag2 == 0.0:
        return 0.0
    val = dot / (mag1 * mag2)
    return max(-1.0, min(1.0, val))


def _is_duplicate_development(report: ResearchReport, developments: list[EventDevelopment]) -> bool:
    norm_report_topic = re.sub(r"\W+", "", report.topic.lower())
    report_desc = report.what_happened or report.executive_summary or ""
    report_hash = hashlib.sha256(re.sub(r"\W+", "", report_desc.lower())[:150].encode("utf-8")).hexdigest()[:16]
    for dev in developments:
        norm_dev_title = re.sub(r"\W+", "", dev.title.lower())
        if norm_report_topic and (norm_report_topic == norm_dev_title or norm_report_topic in norm_dev_title or norm_dev_title in norm_report_topic):
            return True
        dev_meta = dev.metadata_ or {}
        if report.id and dev_meta.get("research_report_id") == report.id:
            return True
        dev_desc = dev.description or ""
        dev_hash = hashlib.sha256(re.sub(r"\W+", "", dev_desc.lower())[:150].encode("utf-8")).hexdigest()[:16]
        if report_hash and dev_hash and report_hash == dev_hash:
            return True
    return False


class EventIntelligenceService:
    def __init__(
        self,
        settings: Settings,
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder or build_embedding_provider(settings)

    async def get_embedding(self, text: str) -> list[float] | None:
        if not text or not text.strip():
            return None
        try:
            res = await self.embedder.embed([text.strip()])
            if not res or len(res) == 0:
                return None
            vec = res[0]
            expected_dim = getattr(self.embedder, "dimensions", 384)
            if len(vec) != expected_dim:
                logger.warning(
                    "embedding_dimension_mismatch",
                    extra={"extra_fields": {"expected": expected_dim, "got": len(vec)}},
                )
                return None
            return vec
        except Exception as exc:
            logger.warning(
                "embedding_provider_failed",
                extra={"extra_fields": {"error": str(exc)}},
            )
            return None

    async def match_or_create_event(
        self,
        session: AsyncSession,
        account_id: int,
        report: ResearchReport,
        *,
        category: str = "geopolitics",
    ) -> tuple[Event, bool, str]:
        """Find matching ongoing event or create a new master event record.

        Returns (event, is_new_event, delta_analysis).
        """
        now = datetime.now(UTC)
        candidate_entities: list[dict[str, Any]] = report.key_entities or []
        candidate_names = {_normalize_name(e.get("name", "")) for e in candidate_entities if e.get("name")}

        candidate_text = f"{report.topic}\n{report.what_happened}"
        candidate_vec = await self.get_embedding(candidate_text)

        # Look back 30 days for active events
        cutoff = now - timedelta(days=30)
        query = (
            select(Event)
            .options(selectinload(Event.entities), selectinload(Event.developments))
            .where(
                Event.account_id == account_id,
                Event.status.in_(["active", "developing"]),
                Event.latest_timestamp >= cutoff,
            )
            .execution_options(populate_existing=True)
        )
        active_events = (await session.scalars(query)).all()

        best_event: Event | None = None
        best_score = 0.0
        best_breakdown: dict[str, Any] = {}

        for ev in active_events:
            ev_names = {_normalize_name(ent.name) for ent in ev.entities if ent.name}
            overlap = _calculate_entity_overlap(candidate_names, ev_names)

            cand_kws = _extract_keywords(f"{report.topic} {report.what_happened}")
            ev_kws = _extract_keywords(f"{ev.title} {ev.summary}")
            inter_kws = cand_kws & ev_kws
            topic_sim = 0.0
            if cand_kws and ev_kws and inter_kws:
                kw_jaccard = len(inter_kws) / len(cand_kws | ev_kws)
                kw_ratio = len(inter_kws) / min(len(cand_kws), len(ev_kws))
                topic_sim = 0.5 * kw_jaccard + 0.5 * kw_ratio

            vec_sim = (
                _cosine_similarity(candidate_vec, ev.embedding)
                if (candidate_vec is not None and ev.embedding is not None)
                else None
            )

            age_days = max(0.0, (now - ev.latest_timestamp).total_seconds() / 86400.0)
            recency_factor = max(0.85, 1.0 - (age_days / 60.0))

            if vec_sim is not None:
                norm_vec = max(0.0, vec_sim)
                composite = (0.45 * norm_vec) + (0.30 * overlap) + (0.25 * topic_sim)
                # Precision bonus when vector similarity and keywords co-occur or actors & keywords strongly co-occur
                if (norm_vec >= 0.70 and len(inter_kws) >= 3) or (overlap >= 0.65 and len(inter_kws) >= 3):
                    composite += 0.10
                score = composite * recency_factor
                is_eligible = score >= 0.55 and (overlap >= 0.25 or len(inter_kws) >= 2)
            else:
                # Safe fallback when embeddings are absent: require both entity/keyword and topic overlap
                composite = (0.50 * overlap) + (0.50 * topic_sim)
                if len(inter_kws) >= 3:
                    composite += 0.15
                elif overlap >= 0.50:
                    composite += 0.10
                score = composite * recency_factor
                is_eligible = score >= 0.50 and (overlap >= 0.30 or len(inter_kws) >= 3)

            if score > best_score:
                best_score = score
                best_event = ev
                best_breakdown = {
                    "score": round(score, 3),
                    "vec_sim": round(vec_sim, 3) if vec_sim is not None else None,
                    "overlap": round(overlap, 3),
                    "topic_sim": round(topic_sim, 3),
                    "is_eligible": is_eligible,
                }

        if best_event is not None and best_breakdown.get("is_eligible", False):
            # Check for duplicate development
            if _is_duplicate_development(report, best_event.developments):
                logger.info(
                    "event_duplicate_development_skipped",
                    extra={"extra_fields": {"event_id": best_event.id, "report_id": report.id}},
                )
                best_event.latest_timestamp = now
                await session.commit()
                return best_event, False, "Duplicate reporting acknowledged; development already recorded"

            delta_type = "continuation"
            topic_lower = report.topic.lower()
            if any(term in topic_lower for term in ["clash", "arrest", "sanction", "banned", "surge", "probe"]):
                delta_type = "escalation"
            elif any(c for c in (report.conflicting_info or [])):
                delta_type = "contradiction"

            dev = EventDevelopment(
                event_id=best_event.id,
                title=report.topic[:512],
                description=report.what_happened or report.executive_summary,
                development_date=now,
                significance_score=report.confidence_score,
                delta_type=delta_type,
                metadata_={
                    "matched_score": round(best_score, 3),
                    "breakdown": best_breakdown,
                    "research_report_id": report.id,
                },
            )
            best_event.developments.append(dev)
            session.add(dev)

            # Link new entities if any
            existing_ent_names = {_normalize_name(ent.name) for ent in best_event.entities if ent.name}
            for ent in candidate_entities:
                name = ent.get("name")
                if name and _normalize_name(name) not in existing_ent_names:
                    session.add(
                        EventEntity(
                            event_id=best_event.id,
                            name=name[:256],
                            entity_type=ent.get("entity_type", "institution"),
                            role=ent.get("role", "subject")[:128],
                        )
                    )

            best_event.latest_timestamp = now
            best_event.status = "developing"
            await session.commit()

            delta_summary = (
                f"Continuation of existing event '{best_event.title}' (score {round(best_score, 2)}). "
                f"Delta: {delta_type} (significance {round(report.confidence_score, 2)})"
            )
            return best_event, False, delta_summary

        # No match above threshold -> Create new Event
        new_event = Event(
            account_id=account_id,
            title=report.topic[:512],
            summary=report.executive_summary or report.what_happened,
            category=category,
            status="active",
            initial_timestamp=now,
            latest_timestamp=now,
            embedding=candidate_vec,
            metadata_={"research_report_id": report.id, "creation_reason": "no_match_above_threshold"},
        )
        session.add(new_event)
        await session.flush()

        # Add initial entities
        for ent in candidate_entities:
            name = ent.get("name")
            if name:
                session.add(
                    EventEntity(
                        event_id=new_event.id,
                        name=name[:256],
                        entity_type=ent.get("entity_type", "institution"),
                        role=ent.get("role", "subject")[:128],
                    )
                )

        # Add initial development
        session.add(
            EventDevelopment(
                event_id=new_event.id,
                title="Event Initiated",
                description=report.what_happened or report.executive_summary,
                development_date=now,
                significance_score=report.confidence_score,
                delta_type="inception",
                metadata_={"research_report_id": report.id},
            )
        )
        await session.commit()
        return new_event, True, "New event created in memory"

    async def link_post_to_event(
        self,
        session: AsyncSession,
        event_id: int,
        post_id: int | None,
        draft_id: int | None,
        context_angle: str = "coverage",
    ) -> EventPostLink:
        """Record published tweet against the event so future cycles don't repeat the take."""
        link = EventPostLink(
            event_id=event_id,
            post_id=post_id,
            draft_id=draft_id,
            context_angle=context_angle,
        )
        session.add(link)
        await session.commit()
        return link

    async def record_editorial_memory(
        self,
        session: AsyncSession,
        account_id: int,
        topic: str,
        angle: str,
        format_name: str,
        hook_type: str = "hook",
        post_id: int | None = None,
        writing_notes: str = "",
    ) -> EditorialMemory:
        """Store editorial take angle so future cycles pick contrasting fresh angles."""
        vec = await self.get_embedding(f"{topic} {angle}")
        mem = EditorialMemory(
            account_id=account_id,
            post_id=post_id,
            angle=angle,
            topic=topic[:256],
            format=format_name,
            hook_type=hook_type,
            writing_notes=writing_notes,
            embedding=vec,
        )
        session.add(mem)
        await session.commit()
        return mem

    async def retrieve_event_memory_context(
        self,
        session: AsyncSession,
        account_id: int,
        event_id: int,
        max_chars: int = 1500,
    ) -> dict[str, Any]:
        """Fetch concise chronological timeline, key entities, and previous takes within a token budget."""
        stmt = (
            select(Event)
            .options(
                selectinload(Event.developments),
                selectinload(Event.post_links),
                selectinload(Event.entities),
            )
            .where(Event.id == event_id)
        )
        event = (await session.scalars(stmt)).first()
        if event is None:
            return {}

        # 1. Timeline developments
        developments = sorted(event.developments, key=lambda d: d.development_date)
        timeline_summaries = []
        for d in developments[-5:]:
            dt_str = d.development_date.strftime("%d %b %H:%M")
            timeline_summaries.append(f"• [{dt_str}] ({d.delta_type.upper()}) {d.description[:220]}")

        # 2. Key entities
        entities_list = [f"{e.name} ({e.role})" for e in event.entities[:6]]

        # 3. Previous coverage / posts
        post_ids = [pl.post_id for pl in event.post_links if pl.post_id]
        past_posts: list[str] = []
        if post_ids:
            posts = (
                await session.scalars(select(Post).where(Post.id.in_(post_ids[-3:])))
            ).all()
            for p in posts:
                past_posts.append(f"• \"{p.text[:160]}...\"")

        # 4. Previous editorial angles
        ed_query = (
            select(EditorialMemory)
            .where(EditorialMemory.account_id == account_id)
            .order_by(desc(EditorialMemory.id))
            .limit(5)
        )
        recent_angles = (await session.scalars(ed_query)).all()
        past_angles = [f"{m.format} ({m.angle}) on {m.topic[:30]}" for m in recent_angles]

        return {
            "event_id": event.id,
            "event_title": event.title,
            "category": event.category,
            "status": event.status,
            "active_since": event.initial_timestamp.strftime("%Y-%m-%d"),
            "entities": entities_list,
            "timeline": timeline_summaries,
            "previous_posts": past_posts,
            "previous_angles": past_angles,
        }

    def format_memory_prompt_block(self, mem: dict[str, Any]) -> str:
        """Format memory context into a compact injection block for the LLM drafting prompt."""
        if not mem:
            return ""

        lines = [
            "\n--- EVENT INTELLIGENCE & TIMELINE MEMORY ---",
            f"Ongoing Story: {mem.get('event_title')} (Active since {mem.get('active_since')})",
        ]

        entities = mem.get("entities", [])
        if entities:
            lines.append(f"Key Entities: {', '.join(entities)}")

        timeline = mem.get("timeline", [])
        if timeline:
            lines.append("Chronological Developments:")
            lines.extend(f"  {t}" for t in timeline)

        prev_posts = mem.get("previous_posts", [])
        if prev_posts:
            lines.append("\nPrevious Tweets Published by @ssh_bugs on this story:")
            lines.extend(f"  {p}" for p in prev_posts)
            lines.append("CRITICAL: DO NOT repeat the same angle or punchline. Advance the story.")

        prev_angles = mem.get("previous_angles", [])
        if prev_angles:
            lines.append(f"Recent Angles Used: {', '.join(prev_angles[:3])}")

        lines.append("------------------------------------------\n")
        return "\n".join(lines)
