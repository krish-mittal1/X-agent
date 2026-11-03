"""Google Gemini (OpenAI-compatible) chat completion provider.

Structured output uses JSON mode plus Pydantic validation, with exactly one
repair request. If a model rejects JSON mode, the request is retried once without
it (still validated). Model identifiers and rate limits come from configuration.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.logging_config import get_logger
from app.net import RetryPolicy, backoff_delay
from app.providers.base import (
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ModelCapabilities,
    PermanentProviderError,
    RateLimitedError,
    StructuredOutputError,
    ToolCall,
    ToolCallResponse,
    ToolSpec,
    TransientProviderError,
    UsageRecord,
)
from app.providers.rate_limit import SlidingWindowLimiter, estimate_tokens

logger = get_logger(__name__)

StructuredT = TypeVar("StructuredT", bound=BaseModel)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
# Models that accept the reasoning_effort parameter; sending it to others is a 400.
_REASONING_MODEL_HINTS = (
    "gemini-2.5",
    "gemini-3",
    "gpt-oss",
    "qwen3",
    "deepseek-r1",
    "o1",
    "o3",
    "o4",
    "thinking",
)


def parse_duration(value: str | None) -> float | None:
    """Parse Gemini / standard duration formats ('570ms', '1m26.4s', '2.5s', '60') into seconds."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    total = 0.0
    matched = False
    for part in _DURATION_PART.finditer(text):
        matched = True
        total += float(part.group("value")) * _DURATION_UNITS[part.group("unit")]
    return total if matched else None


def supports_reasoning_effort(model: str) -> bool:
    lowered = (model or "").lower()
    return any(hint in lowered for hint in _REASONING_MODEL_HINTS)


def extract_json_object(text: str) -> str:
    """Pull the outermost JSON object from a model response."""
    match = _JSON_FENCE.search(text)
    candidate = match.group(1).strip() if match else text.strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return candidate[start : end + 1]


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        usage_hook: Callable[[UsageRecord], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self.model = settings.gemini_model
        self._sleep = sleep
        self.usage_hook = usage_hook
        self._api_key = (
            settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.gemini_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers={"User-Agent": "tech-x-agent/0.1"},
            verify=True,
        )
        # Google Gemini 2.5 Flash offers 1M token context window and structured JSON support
        self.capabilities = ModelCapabilities(
            supports_json_mode=True,
            supports_tool_calling=True,
            context_window=1_048_576,
            max_output_tokens=65536,
        )
        self.limiter = SlidingWindowLimiter(
            requests_per_minute=settings.llm_requests_per_minute,
            tokens_per_minute=settings.llm_tokens_per_minute,
            requests_per_day=settings.llm_requests_per_day,
            tokens_per_day=settings.llm_tokens_per_day,
            safety_ratio=settings.llm_rate_limit_safety_ratio,
            enabled=settings.llm_rate_limit_enabled,
            sleep=sleep,
        )
        self.latest_rate_limits: dict[str, Any] = {
            "limit_requests": settings.llm_requests_per_minute,
            "limit_tokens": settings.llm_tokens_per_minute,
            "limit_day": settings.llm_requests_per_day,
        }

    def get_rate_limits(self) -> dict[str, Any]:
        return dict(self.latest_rate_limits)

    def _record_headers(self, response: httpx.Response) -> None:
        headers = response.headers
        for key in [
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
            "retry-after",
        ]:
            if key in headers:
                short = key.replace("x-ratelimit-", "").replace("-", "_")
                self.latest_rate_limits[short] = headers[key]

    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _estimate_cost(self, usage: LLMUsage) -> float:
        return round(
            (usage.prompt_tokens / 1_000_000) * self._settings.llm_input_price_per_mtok
            + (usage.completion_tokens / 1_000_000) * self._settings.llm_output_price_per_mtok,
            8,
        )

    async def _notify_usage(self, record: UsageRecord) -> None:
        if self.usage_hook is None:
            return
        try:
            await self.usage_hook(record)
        except Exception:  # never let bookkeeping break inference
            logger.exception("usage hook failed")

    # ------------------------------------------------------------------
    async def validate_model(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "GEMINI_API_KEY is not configured"
        if not self.model:
            return False, "GEMINI_MODEL is not configured"
        try:
            response = await self._client.get(
                "/models", headers={"Authorization": f"Bearer {self._api_key}"}
            )
        except httpx.HTTPError as exc:
            return False, f"model catalogue unreachable: {type(exc).__name__}"
        if response.status_code in (401, 403):
            return False, f"Gemini rejected the API key ({response.status_code})"
        if response.status_code >= 400:
            return False, f"model catalogue returned HTTP {response.status_code}"
        try:
            raw_ids = [item.get("id", "") for item in response.json().get("data", [])]
            # Handle both 'gemini-2.5-flash' and 'models/gemini-2.5-flash' formats
            ids = set(raw_ids) | {i.removeprefix("models/") for i in raw_ids if i}
        except ValueError:
            return False, "model catalogue returned invalid JSON"
        target_model = self.model.removeprefix("models/")
        if target_model not in ids and self.model not in ids:
            sample = ", ".join(sorted(ids)[:15]) or "<empty>"
            return False, f"model {self.model!r} not available; catalogue sample: {sample}"
        return True, f"model {self.model!r} available"

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _parse_retry_after(self, response: httpx.Response) -> float | None:
        """Seconds to wait, from retry-after or x-ratelimit-reset-* headers."""
        for header in (
            "retry-after",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-reset-requests",
        ):
            delay = parse_duration(response.headers.get(header))
            if delay is not None:
                return delay
        return None

    async def _chat(
        self,
        messages: list[LLMMessage],
        *,
        operation: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[LLMResponse, str]:
        """One chat completion with bounded retries. Returns (response, raw_content)."""
        if not self._api_key:
            raise PermanentProviderError("GEMINI_API_KEY is not configured")
        if not self.model:
            raise PermanentProviderError("GEMINI_MODEL is not configured")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._settings.llm_max_output_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if self._settings.llm_reasoning_effort and supports_reasoning_effort(self.model):
            payload["reasoning_effort"] = self._settings.llm_reasoning_effort

        # Output tokens count against the per-minute budget too
        estimated_tokens = estimate_tokens(
            json.dumps(payload), self._settings.llm_chars_per_token
        ) + int(payload.get("max_tokens") or 0)

        policy = RetryPolicy(attempts=max(1, self._settings.llm_max_retries + 1))
        last_error: LLMError | None = None
        for attempt in range(1, policy.attempts + 1):
            started = time.perf_counter()
            reservation = await self.limiter.acquire(estimated_tokens)
            try:
                response = await self._client.post(
                    "/chat/completions", json=payload, headers=self._headers()
                )
            except httpx.TimeoutException as exc:
                if reservation is not None:
                    reservation.settle(0)
                last_error = TransientProviderError(f"Gemini request timed out: {exc}")
                await self._notify_usage(
                    UsageRecord(
                        self.name,
                        self.model,
                        operation,
                        0,
                        0,
                        (time.perf_counter() - started) * 1000,
                        0.0,
                        False,
                        "timeout",
                    )
                )
            except httpx.HTTPError as exc:
                if reservation is not None:
                    reservation.settle(0)
                last_error = TransientProviderError(
                    f"Gemini transport error: {type(exc).__name__}"
                )
                await self._notify_usage(
                    UsageRecord(
                        self.name,
                        self.model,
                        operation,
                        0,
                        0,
                        (time.perf_counter() - started) * 1000,
                        0.0,
                        False,
                        "transport",
                    )
                )
            else:
                self._record_headers(response)
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response)
                    if reservation is not None:
                        reservation.settle(0)
                    last_error = RateLimitedError("Gemini rate limit exceeded", retry_after)
                    await self._notify_usage(
                        UsageRecord(
                            self.name,
                            self.model,
                            operation,
                            0,
                            0,
                            (time.perf_counter() - started) * 1000,
                            0.0,
                            False,
                            "rate_limited",
                        )
                    )
                elif response.status_code >= 500:
                    if reservation is not None:
                        reservation.settle(0)
                    last_error = TransientProviderError(
                        f"Gemini server error {response.status_code}"
                    )
                    await self._notify_usage(
                        UsageRecord(
                            self.name,
                            self.model,
                            operation,
                            0,
                            0,
                            (time.perf_counter() - started) * 1000,
                            0.0,
                            False,
                            "server_error",
                        )
                    )
                elif response.status_code in (401, 403):
                    detail = response.text[:500]
                    raise PermanentProviderError(
                        f"Gemini authentication failed ({response.status_code}): {detail}"
                    )
                elif response.status_code >= 400:
                    detail = response.text[:500]
                    if response.status_code == 400 and response_format is not None and (
                        "response_format" in detail or "json" in detail.lower()
                    ):
                        raise _JsonModeUnsupported(detail)
                    if "safety" in detail.lower() or "blocked" in detail.lower():
                        raise PermanentProviderError(
                            f"Gemini safety filter triggered ({response.status_code}): {detail}"
                        )
                    raise PermanentProviderError(
                        f"Gemini rejected the request ({response.status_code}): {detail}"
                    )
                else:
                    latency_ms = (time.perf_counter() - started) * 1000
                    try:
                        data = response.json()
                        choices = data.get("choices", [])
                        if not choices:
                            raise ValueError("no choices returned in Gemini response")
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason")
                        if finish_reason and finish_reason.lower() in (
                            "safety",
                            "content_filter",
                            "recitation",
                            "blocklist",
                        ):
                            raise PermanentProviderError(
                                f"Gemini response blocked by content/safety filter (finish_reason={finish_reason})"
                            )
                        message = choice.get("message", {})
                        content = message.get("content") or ""
                        usage = LLMUsage(
                            prompt_tokens=int(data.get("usage", {}).get("prompt_tokens", 0)),
                            completion_tokens=int(
                                data.get("usage", {}).get("completion_tokens", 0)
                            ),
                        )
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                        raise TransientProviderError(f"malformed Gemini response: {exc}") from exc

                    if reservation is not None:
                        reservation.settle(usage.prompt_tokens + usage.completion_tokens)
                    result = LLMResponse(
                        text=content,
                        model=data.get("model", self.model),
                        usage=usage,
                        latency_ms=latency_ms,
                        estimated_cost_usd=self._estimate_cost(usage),
                        finish_reason=finish_reason,
                        attempts=attempt,
                        raw={"id": data.get("id"), "tool_calls": message.get("tool_calls") or []},
                    )
                    await self._notify_usage(
                        UsageRecord(
                            self.name,
                            result.model,
                            operation,
                            usage.prompt_tokens,
                            usage.completion_tokens,
                            latency_ms,
                            result.estimated_cost_usd,
                            True,
                        )
                    )
                    return result, content

            if attempt >= policy.attempts:
                break
            delay = (
                last_error.retry_after
                if isinstance(last_error, RateLimitedError) and last_error.retry_after
                else backoff_delay(policy, attempt)
            )
            logger.warning(
                "Retrying Gemini request",
                extra={
                    "extra_fields": {
                        "attempt": attempt,
                        "delay_s": round(delay, 2),
                        "error": str(last_error)[:200],
                    }
                },
            )
            await self._sleep(delay)

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    async def generate(
        self,
        messages: list[LLMMessage],
        *,
        operation: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        response, _ = await self._chat(
            messages, operation=operation, temperature=temperature, max_tokens=max_tokens
        )
        return response

    async def generate_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        *,
        operation: str = "tools",
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        if not tools:
            raise PermanentProviderError("generate_with_tools requires at least one tool")
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]
        response, _ = await self._chat(
            messages,
            operation=operation,
            tools=definitions,
            tool_choice=tool_choice,
        )
        calls: list[ToolCall] = []
        for item in response.raw.get("tool_calls", []):
            function = item.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    raise StructuredOutputError(
                        "tool call arguments were not valid JSON"
                    ) from None
            calls.append(
                ToolCall(
                    id=str(item.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return ToolCallResponse(response=response, tool_calls=calls)

    async def generate_structured(
        self,
        messages: list[LLMMessage],
        schema: type[StructuredT],
        *,
        operation: str = "structured",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[StructuredT, LLMResponse]:
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        system_suffix = (
            "\n\nReturn ONLY a single JSON object that validates against this JSON Schema. "
            "No prose, no markdown fences.\n"
            f"JSON Schema:\n{schema_json}"
        )
        prepared = list(messages)
        if prepared and prepared[0].role == "system":
            prepared[0] = LLMMessage("system", prepared[0].content + system_suffix)
        else:
            prepared.insert(0, LLMMessage("system", system_suffix.strip()))

        json_mode = {"type": "json_object"}
        try:
            response, raw = await self._chat(
                prepared,
                operation=operation,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=json_mode,
            )
        except _JsonModeUnsupported as exc:
            logger.warning(
                "Provider rejected JSON mode; retrying without response_format",
                extra={"extra_fields": {"detail": str(exc)[:200]}},
            )
            response, raw = await self._chat(
                prepared,
                operation=f"{operation}:no_json_mode",
                temperature=temperature,
                max_tokens=max_tokens,
            )

        parsed = self._validate(schema, raw)
        if parsed is not None:
            return parsed, response

        # Exactly one repair request, including the validation error.
        try:
            first_error = extract_json_object(raw)
            schema.model_validate_json(first_error)
        except (ValueError, ValidationError) as exc:
            error_text = str(exc)[:1500]
        else:  # pragma: no cover - defensive
            error_text = "unknown validation failure"
        repair_messages = prepared + [
            LLMMessage("assistant", raw),
            LLMMessage(
                "user",
                "That response did not validate against the schema. Error:\n"
                f"{error_text}\nReturn only corrected JSON.",
            ),
        ]
        repair_response, repaired_raw = await self._chat(
            repair_messages,
            operation=f"{operation}:repair",
            temperature=0.1,
            max_tokens=max_tokens,
            response_format=json_mode,
        )
        parsed = self._validate(schema, repaired_raw)
        if parsed is None:
            raise StructuredOutputError(
                f"model output failed {schema.__name__} validation after repair",
                raw_text=repaired_raw,
            )
        return parsed, repair_response

    @staticmethod
    def _validate(schema: type[StructuredT], raw: str) -> StructuredT | None:
        try:
            return schema.model_validate_json(extract_json_object(raw))
        except (ValueError, ValidationError) as exc:
            logger.warning(
                "structured output invalid",
                extra={"extra_fields": {"error": str(exc)[:300]}},
            )
            return None


class _JsonModeUnsupported(Exception):
    pass


async def validate_provider_startup(provider: GeminiProvider) -> None:
    ok, detail = await provider.validate_model()
    if ok:
        logger.info("LLM model validated", extra={"extra_fields": {"detail": detail}})
    else:
        logger.error(
            "LLM model not validated; inference will fail until fixed",
            extra={"extra_fields": {"detail": detail}},
        )

