from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.memory import database as db_module
from app.memory.database import session_scope
from app.memory.models import ModelUsageRecord
from app.providers.base import BudgetExceededError, LLMMessage, LLMResponse, LLMUsage
from app.providers.factory import LLMService


class FakeProvider:
    name = "fake"
    model = "fake-model"
    capabilities = None

    def __init__(self) -> None:
        self.usage_hook = None
        self.calls = 0

    async def generate(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return LLMResponse(
            text="ok", model="fake-model", usage=LLMUsage(10, 5), latency_ms=1.0,
            estimated_cost_usd=0.001,
        )

    async def generate_structured(self, messages, schema, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def validate_model(self):  # type: ignore[no-untyped-def]
        return True, "ok"

    async def aclose(self) -> None:
        return None


async def test_request_budget_is_enforced(settings, engine, account) -> None:
    limited = settings.model_copy(update={"llm_max_requests_per_day": 1})
    async with session_scope() as session:
        session.add(
            ModelUsageRecord(
                account_id=account.id,
                provider="fake",
                model="fake-model",
                operation="test",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                latency_ms=1.0,
                estimated_cost_usd=0.0,
                success=True,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    provider = FakeProvider()
    service = LLMService(limited, provider, db_module.get_session_factory(), account.id)  # type: ignore[arg-type]
    with pytest.raises(BudgetExceededError, match="request budget"):
        await service.generate([LLMMessage("user", "hi")])
    assert provider.calls == 0


async def test_daily_spend_budget_is_enforced(settings, engine, account) -> None:
    limited = settings.model_copy(
        update={"llm_daily_budget_usd": 0.5, "llm_max_requests_per_day": 0}
    )
    async with session_scope() as session:
        session.add(
            ModelUsageRecord(
                account_id=account.id,
                provider="fake",
                model="fake-model",
                operation="test",
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                latency_ms=1.0,
                estimated_cost_usd=0.75,
                success=True,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    provider = FakeProvider()
    service = LLMService(limited, provider, db_module.get_session_factory(), account.id)  # type: ignore[arg-type]
    with pytest.raises(BudgetExceededError, match="spend budget"):
        await service.generate([LLMMessage("user", "hi")])
    assert provider.calls == 0


async def test_successful_call_records_usage(settings, engine, account) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    provider = FakeProvider()
    service = LLMService(settings, provider, db_module.get_session_factory(), account.id)  # type: ignore[arg-type]
    assert provider.usage_hook is not None
    response = await service.provider.generate([LLMMessage("user", "hi")])
    assert response.text == "ok"
    from app.providers.base import UsageRecord

    await provider.usage_hook(
        UsageRecord("fake", "fake-model", "test", 10, 5, 1.0, 0.001, True)
    )
    async with session_scope() as session:
        count = await session.scalar(select(func.count(ModelUsageRecord.id)))
    assert count == 1
