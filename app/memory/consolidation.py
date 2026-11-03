"""Memory consolidation (separate, explicit maintenance process).

Consolidation never promotes model output to verified fact. It only expires stale
conversation memories, invalidates semantic memories whose source failed to
retrieve, and clamps over-confident semantic memories to their source reliability.
Verified evidence lives in ``evidence_claims`` and is not touched here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import MEMORY_SEMANTIC, MemoryRecord, SourceDocument
from app.memory.vector_store import MemoryVectorStore

logger = get_logger(__name__)


class MemoryConsolidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = MemoryVectorStore(settings)

    async def run(self, session: AsyncSession, account_id: int) -> dict[str, Any]:
        expired = await self.store.expire_conversation_memory(
            session, account_id, self.settings.memory_conversation_ttl_days
        )

        records = list(
            (
                await session.scalars(
                    select(MemoryRecord).where(
                        MemoryRecord.account_id == account_id,
                        MemoryRecord.category == MEMORY_SEMANTIC,
                        MemoryRecord.invalidated_at.is_(None),
                    )
                )
            ).all()
        )
        source_ids = {r.source_document_id for r in records if r.source_document_id}
        documents: dict[int, SourceDocument] = {}
        if source_ids:
            rows = await session.scalars(
                select(SourceDocument).where(SourceDocument.id.in_(source_ids))
            )
            documents = {doc.id: doc for doc in rows}

        invalidated = 0
        confidence_clamped = 0
        now = datetime.now(UTC)
        for record in records:
            document = documents.get(record.source_document_id) if record.source_document_id else None
            if record.source_document_id and document is None:
                record.invalidated_at = now
                record.confidence = 0.0
                invalidated += 1
                continue
            if document is not None and document.fetch_status == "failed":
                record.invalidated_at = now
                record.confidence = 0.0
                invalidated += 1
                continue
            reliability = float(record.provenance.get("reliability", record.confidence) or 0.0)
            if reliability < 0.4 and record.confidence > reliability:
                record.confidence = reliability
                confidence_clamped += 1

        summary = {
            "expired_conversation_memories": expired,
            "invalidated_semantic_memories": invalidated,
            "confidence_clamped": confidence_clamped,
            "promoted_to_verified_fact": False,
            "ran_at": now.isoformat(),
        }
        logger.info("memory consolidation complete", extra={"extra_fields": summary})
        return summary
