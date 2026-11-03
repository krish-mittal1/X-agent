"""Provider construction and budget-aware inference service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import ModelUsageRecord
from app.providers.base import (
    BudgetExceededError,
    DisabledProvider,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    UsageRecord,
)
from app.providers.gemini_provider import GeminiProvider

logger = get_logger(__name__)

StructuredT = TypeVar("StructuredT", bound=BaseModel)


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "disabled":
        logger.info("LLM provider disabled by configuration")
        return DisabledProvider()
    if not settings.gemini_api_key or not settings.gemini_model:
        logger.warning(
            "Gemini provider not fully configured (GEMINI_API_KEY/GEMINI_MODEL missing); "
            "running in degraded mode, drafts will be queued"
        )
        return DisabledProvider()
    return GeminiProvider(settings)


async def budget_status(
    session: AsyncSession, settings: Settings, account_id: int | None
) -> dict[str, float]:
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    async def _spend(since: datetime) -> float:
        stmt = select(func.coalesce(func.sum(ModelUsageRecord.estimated_cost_usd), 0.0)).where(
            ModelUsageRecord.created_at >= since
        )
        if account_id is not None:
            stmt = stmt.where(ModelUsageRecord.account_id == account_id)
        return float(await session.scalar(stmt) or 0.0)

    async def _requests(since: datetime) -> int:
        stmt = select(func.count(ModelUsageRecord.id)).where(
            ModelUsageRecord.created_at >= since
        )
        if account_id is not None:
            stmt = stmt.where(ModelUsageRecord.account_id == account_id)
        return int(await session.scalar(stmt) or 0)

    return {
        "spend_today_usd": await _spend(day_start),
        "spend_month_usd": await _spend(month_start),
        "requests_today": await _requests(day_start),
    }


class LLMService:
    """Budget enforcement + usage persistence around a provider."""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        session_factory: async_sessionmaker[AsyncSession],
        account_id: int | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self._session_factory = session_factory
        self.account_id = account_id
        hook = getattr(provider, "usage_hook", None)
        if hook is not None:
            provider.usage_hook = self._record_usage  # type: ignore[attr-defined]

    @property
    def enabled(self) -> bool:
        return not isinstance(self.provider, DisabledProvider)

    async def check_budget(self) -> None:
        if not self.enabled:
            return
        async with self._session_factory() as session:
            status = await budget_status(session, self.settings, self.account_id)
        if self.settings.llm_max_requests_per_day > 0 and status[
            "requests_today"
        ] >= self.settings.llm_max_requests_per_day:
            raise BudgetExceededError(
                f"daily LLM request budget exhausted ({int(status['requests_today'])} requests)"
            )
        if self.settings.llm_daily_budget_usd > 0 and status[
            "spend_today_usd"
        ] >= self.settings.llm_daily_budget_usd:
            raise BudgetExceededError(
                f"daily LLM spend budget exhausted (${status['spend_today_usd']:.4f})"
            )
        if self.settings.llm_monthly_budget_usd > 0 and status[
            "spend_month_usd"
        ] >= self.settings.llm_monthly_budget_usd:
            raise BudgetExceededError(
                f"monthly LLM spend budget exhausted (${status['spend_month_usd']:.4f})"
            )

    async def _record_usage(self, record: UsageRecord) -> None:
        async with self._session_factory() as session:
            session.add(
                ModelUsageRecord(
                    account_id=self.account_id,
                    provider=record.provider,
                    model=record.model,
                    operation=record.operation,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    total_tokens=record.prompt_tokens + record.completion_tokens,
                    latency_ms=record.latency_ms,
                    estimated_cost_usd=record.estimated_cost_usd,
                    success=record.success,
                    error_type=record.error_type,
                    correlation_id=None,
                )
            )
            await session.commit()

    async def generate(
        self,
        messages: list[LLMMessage],
        *,
        operation: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        await self.check_budget()
        return await self.provider.generate(
            messages, operation=operation, temperature=temperature, max_tokens=max_tokens
        )

    async def generate_structured(
        self,
        messages: list[LLMMessage],
        schema: type[StructuredT],
        *,
        operation: str = "structured",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[StructuredT, LLMResponse]:
        await self.check_budget()
        return await self.provider.generate_structured(
            messages, schema, operation=operation, temperature=temperature, max_tokens=max_tokens
        )

    async def aclose(self) -> None:
        await self.provider.aclose()

    def get_rate_limit_status(self) -> dict[str, Any]:
        if hasattr(self.provider, "get_rate_limits"):
            return self.provider.get_rate_limits()
        return {}


async def usage_snapshot(
    session: AsyncSession, days: int = 7
) -> list[dict[str, object]]:
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(
            ModelUsageRecord.model,
            ModelUsageRecord.operation,
            func.count(ModelUsageRecord.id),
            func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0),
            func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0),
            func.coalesce(func.sum(ModelUsageRecord.estimated_cost_usd), 0.0),
        )
        .where(ModelUsageRecord.created_at >= since)
        .group_by(ModelUsageRecord.model, ModelUsageRecord.operation)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "model": row[0],
            "operation": row[1],
            "calls": int(row[2]),
            "prompt_tokens": int(row[3]),
            "completion_tokens": int(row[4]),
            "estimated_cost_usd": round(float(row[5]), 6),
        }
        for row in rows
    ]


UsageHook = Callable[[UsageRecord], Awaitable[None]]
