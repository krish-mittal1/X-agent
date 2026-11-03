from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.config import Settings
from app.providers.base import (
    LLMMessage,
    PermanentProviderError,
    StructuredOutputError,
    ToolSpec,
    TransientProviderError,
    UsageRecord,
)
from app.providers.gemini_provider import (
    GeminiProvider,
    extract_json_object,
    parse_duration,
    supports_reasoning_effort,
)


class Draft(BaseModel):
    headline: str
    confidence: float


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "gemini_api_key": SecretStr("test-key"),
        "gemini_model": "test-model",
        "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "llm_max_retries": 2,
        "llm_input_price_per_mtok": 1.0,
        "llm_output_price_per_mtok": 2.0,
        "llm_max_output_tokens": 256,
    }
    base.update(overrides)
    return Settings(**base)


def completion(
    content: str,
    *,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def make_provider(handler, *, settings: Settings | None = None, sleep=None):  # type: ignore[no-untyped-def]
    settings = settings or make_settings()
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )
    usage: list[UsageRecord] = []

    async def hook(record: UsageRecord) -> None:
        usage.append(record)

    async def fake_sleep(delay: float) -> None:
        if sleep is not None:
            sleep.append(delay)

    provider = GeminiProvider(settings, client=client, sleep=fake_sleep, usage_hook=hook)
    return provider, client, usage


async def test_generate_parses_usage_and_cost() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        return httpx.Response(200, json=completion("hello world"))

    provider, client, usage = make_provider(handler)
    try:
        response = await provider.generate([LLMMessage("user", "hi")], operation="unit")
    finally:
        await client.aclose()
    assert response.text == "hello world"
    assert response.usage.prompt_tokens == 1000
    assert response.estimated_cost_usd == pytest.approx(0.002)
    assert usage[-1].success is True
    assert usage[-1].operation == "unit"


async def test_rate_limit_is_retried_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "slow down"})
        return httpx.Response(200, json=completion("ok"))

    provider, client, usage = make_provider(handler, sleep=sleeps)
    try:
        response = await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert response.text == "ok"
    assert response.attempts == 2
    assert sleeps == [2.0]
    assert usage[0].success is False
    assert usage[0].error_type == "rate_limited"


async def test_server_errors_exhaust_retries() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    provider, client, usage = make_provider(handler, sleep=sleeps)
    try:
        with pytest.raises(TransientProviderError):
            await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert calls["n"] == 3  # 1 initial + LLM_MAX_RETRIES
    assert len(sleeps) == 2
    assert all(not record.success for record in usage)


async def test_auth_errors_are_not_retried() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    provider, client, _ = make_provider(handler)
    try:
        with pytest.raises(PermanentProviderError):
            await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert calls["n"] == 1


async def test_safety_filter_blocks_response_with_clear_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion("", finish_reason="SAFETY"))

    provider, client, _ = make_provider(handler)
    try:
        with pytest.raises(PermanentProviderError, match="filter"):
            await provider.generate([LLMMessage("user", "controversial prompt")])
    finally:
        await client.aclose()


async def test_structured_output_validates() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=completion('```json\n{"headline": "ok", "confidence": 0.9}\n```')
        )

    provider, client, _ = make_provider(handler)
    try:
        model, response = await provider.generate_structured(
            [LLMMessage("system", "write"), LLMMessage("user", "write json")], Draft
        )
    finally:
        await client.aclose()
    assert model.headline == "ok"
    assert response.text.startswith("```")


async def test_structured_output_repairs_once_then_fails() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=completion('{"headline": "x"}'))

    provider, client, _ = make_provider(handler)
    try:
        with pytest.raises(StructuredOutputError):
            await provider.generate_structured(
                [LLMMessage("system", "write"), LLMMessage("user", "write json")], Draft
            )
    finally:
        await client.aclose()
    assert calls["n"] == 2  # initial + single repair


async def test_structured_output_repair_succeeds() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=completion('{"headline": "x"}'))
        return httpx.Response(200, json=completion('{"headline": "x", "confidence": 0.4}'))

    provider, client, _ = make_provider(handler)
    try:
        model, _ = await provider.generate_structured(
            [LLMMessage("system", "write"), LLMMessage("user", "write json")], Draft
        )
    finally:
        await client.aclose()
    assert model.confidence == 0.4
    assert calls["n"] == 2


async def test_json_mode_rejection_falls_back_without_response_format() -> None:
    seen_formats: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_formats.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format json_object not supported")
        return httpx.Response(200, json=completion('{"headline": "ok", "confidence": 0.5}'))

    provider, client, _ = make_provider(handler)
    try:
        model, _ = await provider.generate_structured(
            [LLMMessage("system", "write"), LLMMessage("user", "json")], Draft
        )
    finally:
        await client.aclose()
    assert model.headline == "ok"
    assert seen_formats == [True, False]


async def test_generate_with_tools_parses_tool_calls() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "lookup_docs"
        assert body["tool_choice"] == "auto"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tool",
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_docs",
                                        "arguments": '{"query": "pgvector"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    provider, client, _ = make_provider(handler)
    try:
        result = await provider.generate_with_tools(
            [LLMMessage("user", "find docs")],
            [
                ToolSpec(
                    name="lookup_docs",
                    description="search docs",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                )
            ],
        )
    finally:
        await client.aclose()
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "lookup_docs"
    assert result.tool_calls[0].arguments == {"query": "pgvector"}


async def test_unconfigured_provider_raises_without_network() -> None:
    provider = GeminiProvider(make_settings(gemini_api_key=None))
    try:
        with pytest.raises(PermanentProviderError, match="GEMINI_API_KEY"):
            await provider.generate([LLMMessage("user", "hi")])
    finally:
        await provider.aclose()


def test_extract_json_object_handles_fences_and_prose() -> None:
    assert extract_json_object('Sure!\n```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json_object('prefix {"a": {"b": 2}} suffix') == '{"a": {"b": 2}}'
    with pytest.raises(ValueError):
        extract_json_object("no json here")


async def test_validate_model_reports_missing_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    provider, client, _ = make_provider(handler)
    try:
        ok, detail = await provider.validate_model()
    finally:
        await client.aclose()
    assert ok is False
    assert "not available" in detail


async def test_validate_model_accepts_configured_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    provider, client, _ = make_provider(handler)
    try:
        ok, detail = await provider.validate_model()
    finally:
        await client.aclose()
    assert ok is True
    assert "test-model" in detail


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2", 2.0),
        ("2.5s", 2.5),
        ("570ms", 0.57),
        ("1m26.4s", 86.4),
        ("1h2m3s", 3723.0),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_duration_handles_gemini_formats(raw: str, expected: float | None) -> None:
    result = parse_duration(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


async def test_429_uses_token_reset_header_as_a_duration() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"x-ratelimit-reset-tokens": "1m26.4s"},
                json={"error": "rate limit"},
            )
        return httpx.Response(200, json=completion("ok"))

    provider, client, _ = make_provider(handler, sleep=sleeps)
    try:
        response = await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert response.text == "ok"
    assert sleeps == [86.4]


def test_supports_reasoning_effort_only_for_reasoning_models() -> None:
    assert supports_reasoning_effort("gemini-2.5-flash")
    assert supports_reasoning_effort("gemini-3")
    assert supports_reasoning_effort("openai/gpt-oss-120b")
    assert supports_reasoning_effort("qwen/qwen3-32b")
    assert not supports_reasoning_effort("gemini-1.5-flash")


async def test_reasoning_effort_is_sent_for_gemini_2_5() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion("ok"))

    settings = make_settings(gemini_model="gemini-2.5-flash", llm_reasoning_effort="low")
    provider, client, _ = make_provider(handler, settings=settings)
    try:
        await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert captured["reasoning_effort"] == "low"


async def test_reasoning_effort_is_omitted_for_plain_models() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion("ok"))

    settings = make_settings(gemini_model="gemini-1.5-flash", llm_reasoning_effort="low")
    provider, client, _ = make_provider(handler, settings=settings)
    try:
        await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert "reasoning_effort" not in captured


async def test_daily_quota_exhaustion_fails_without_calling_the_api() -> None:
    from app.providers.rate_limit import RateLimitExceededError

    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=completion("ok"))

    settings = make_settings(llm_requests_per_day=1)
    provider, client, _ = make_provider(handler, settings=settings)
    try:
        await provider.generate([LLMMessage("user", "hi")])
        with pytest.raises(RateLimitExceededError):
            await provider.generate([LLMMessage("user", "hi")])
    finally:
        await client.aclose()
    assert calls["n"] == 1
