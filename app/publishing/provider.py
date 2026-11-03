"""Publishing provider abstraction.

Decouples content generation, ranking, and validation from the concrete delivery channel.
In local_draft mode, LocalDraftProvider keeps content staged locally in the database review queue
without initializing an X OAuth client or making outbound network calls to api.x.com.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import (
    DRAFT_APPROVED,
    DRAFT_PUBLISHED,
    Draft,
)
from app.memory.repositories import AuditRepository, PostRepository
from app.schemas.publishing import PolicyDecision, PublishResult

logger = get_logger(__name__)


class PublishingProvider(ABC):
    """Abstract base provider for draft publishing."""

    @abstractmethod
    async def publish(
        self,
        draft: Draft,
        parts: list[str],
        *,
        decision: PolicyDecision | None = None,
    ) -> PublishResult:
        """Deliver the post through the provider."""

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        """Return provider readiness and configuration status."""

    @abstractmethod
    async def validate_connection(self) -> dict[str, Any]:
        """Validate connection / credentials status without publishing."""


class LocalDraftProvider(PublishingProvider):
    """Default local staging provider.

    Saves approved content locally in the database review queue for operator dispatch.
    Decoupled from api.x.com, ensuring content is staged safely for manual review.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock=None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.clock = clock or (lambda: datetime.now(UTC))

    async def publish(
        self,
        draft: Draft,
        parts: list[str],
        *,
        decision: PolicyDecision | None = None,
    ) -> PublishResult:
        now = self.clock()
        async with self.session_factory() as session:
            db_draft = await session.get(Draft, draft.id)
            if db_draft is not None:
                db_draft.status = DRAFT_APPROVED
                db_draft.reviewed_at = now
                policy = dict(db_draft.policy_report or {})
                policy["zero_cost_published"] = True
                policy["publishing_mode"] = "local_draft"
                db_draft.policy_report = policy

            await AuditRepository.log(
                session,
                action="publish_local_draft",
                outcome="staged",
                account_id=draft.account_id,
                details={
                    "draft_id": draft.id,
                    "parts": len(parts),
                    "zero_cost": True,
                    "provider": "local_draft",
                },
            )
            await session.commit()

        logger.info(
            "draft staged in local review queue",
            extra={"extra_fields": {"draft_id": draft.id, "parts": len(parts)}},
        )
        return PublishResult(
            status="local_saved",
            draft_id=draft.id,
            post_ids=[],
            reasons=["saved to local review queue"],
            policy=decision,
        )

    async def get_status(self) -> dict[str, Any]:
        return {
            "provider": "local_draft",
            "zero_cost": True,
            "cost_inr": 0.0,
            "cost_usd": 0.0,
            "outbound_requests_allowed": False,
            "description": "Local Review & Operator Dispatch Queue",
        }

    async def validate_connection(self) -> dict[str, Any]:
        return {
            "valid": True,
            "provider": "local_draft",
            "zero_cost": True,
            "detail": "Local staging queue active. Outbound external API calls decoupled.",
        }


class XOfficialAPIProvider(PublishingProvider):
    """Optional implementation for official X API publishing (requires paid tier / credits).

    Disabled by default. Refuses to execute if zero-cost mode is active.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        x_client,
        *,
        clock=None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.x_client = x_client
        self.clock = clock or (lambda: datetime.now(UTC))

    def _ensure_allowed(self) -> None:
        if self.settings.is_zero_cost_mode or not self.settings.x_api_enabled:
            raise RuntimeError(
                "XOfficialAPIProvider cannot be used when zero-cost mode is active "
                "(X_PUBLISHING_MODE='local_draft' or X_API_ENABLED=false)."
            )
        if not self.settings.publishing_enabled or self.settings.dry_run:
            raise RuntimeError("Live X publishing is not enabled in settings.")

    async def publish(
        self,
        draft: Draft,
        parts: list[str],
        *,
        decision: PolicyDecision | None = None,
    ) -> PublishResult:
        self._ensure_allowed()
        if self.x_client is None:
            raise RuntimeError("XClient is not initialized.")

        post_ids: list[str] = []
        in_reply_to: str | None = None
        now = self.clock()
        async with self.session_factory() as session:
            for position, part in enumerate(parts):
                post_record = await self.x_client.create_post(part, in_reply_to=in_reply_to)
                post_ids.append(post_record.id)
                in_reply_to = post_record.id
                await PostRepository.create(
                    session,
                    account_id=draft.account_id,
                    x_post_id=post_record.id,
                    text=post_record.text,
                    draft_id=draft.id,
                    position=position,
                    published_at=now,
                )
            db_draft = await session.get(Draft, draft.id)
            if db_draft:
                db_draft.status = DRAFT_PUBLISHED
                db_draft.published_at = now
            await session.commit()
        return PublishResult(
            status="published",
            draft_id=draft.id,
            post_ids=post_ids,
            policy=decision,
        )

    async def get_status(self) -> dict[str, Any]:
        return {
            "provider": "official_api",
            "zero_cost": False,
            "configured": bool(self.x_client and getattr(self.x_client, "configured", False)),
            "outbound_requests_allowed": self.settings.publishing_enabled and not self.settings.dry_run,
        }

    async def validate_connection(self) -> dict[str, Any]:
        if not self.x_client or not getattr(self.x_client, "configured", False):
            return {"valid": False, "detail": "XClient not configured"}
        return {"valid": True, "detail": "XClient credentials present"}


def get_publishing_provider(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    x_client=None,
    *,
    clock=None,
) -> PublishingProvider:
    """Factory selecting the appropriate publishing provider."""
    # Strict lockdown: local_draft mode or disabled/dry-run publishing must NEVER instantiate BraveCDPPublisher
    if settings.x_publishing_mode == "local_draft" or not settings.publishing_enabled or settings.dry_run:
        return LocalDraftProvider(settings, session_factory, clock=clock)
    if settings.x_publishing_mode == "brave_cdp":
        from app.publishing.brave_cdp import BraveCDPPublisher

        return BraveCDPPublisher(settings, session_factory, clock=clock)
    if settings.x_publishing_mode == "official_api" and settings.x_api_enabled:
        return XOfficialAPIProvider(settings, session_factory, x_client, clock=clock)
    return LocalDraftProvider(settings, session_factory, clock=clock)
