"""Vendor-neutral LLM provider interface and error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

StructuredT = TypeVar("StructuredT", bound=BaseModel)


class LLMError(RuntimeError):
    """Base class for inference failures."""


class ProviderUnavailableError(LLMError):
    """No provider is configured or reachable; work should be queued for later."""


class BudgetExceededError(LLMError):
    """Configured spending or request budget is exhausted."""


class RateLimitedError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientProviderError(LLMError):
    """Retryable transport/server failure."""


class PermanentProviderError(LLMError):
    """Non-retryable request error (bad request, auth, model missing)."""


class StructuredOutputError(LLMError):
    """Model output did not validate against the requested Pydantic schema."""

    def __init__(self, message: str, raw_text: str = "", errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.errors = errors or []


@dataclass(frozen=True)
class ModelCapabilities:
    supports_json_mode: bool
    supports_tool_calling: bool
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage
    latency_ms: float
    estimated_cost_usd: float
    finish_reason: str | None = None
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallResponse:
    response: LLMResponse
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    estimated_cost_usd: float
    success: bool
    error_type: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    capabilities: ModelCapabilities

    async def generate(
        self,
        messages: list[LLMMessage],
        *,
        operation: str = "generate",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def generate_structured(
        self,
        messages: list[LLMMessage],
        schema: type[StructuredT],
        *,
        operation: str = "structured",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[StructuredT, LLMResponse]: ...

    async def generate_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        *,
        operation: str = "tools",
        tool_choice: str = "auto",
    ) -> ToolCallResponse: ...

    async def validate_model(self) -> tuple[bool, str]: ...

    async def aclose(self) -> None: ...


class DisabledProvider:
    """Explicitly disabled inference: callers queue work instead of failing the app."""

    name = "disabled"
    model = "none"
    capabilities = ModelCapabilities(supports_json_mode=False, supports_tool_calling=False)

    async def generate(self, messages: list[LLMMessage], **_: Any) -> LLMResponse:
        raise ProviderUnavailableError("LLM provider is disabled or not configured")

    async def generate_structured(self, messages: list[LLMMessage], schema: type[StructuredT], **_: Any):
        raise ProviderUnavailableError("LLM provider is disabled or not configured")

    async def generate_with_tools(self, messages: list[LLMMessage], tools: list[ToolSpec], **_: Any):
        raise ProviderUnavailableError("LLM provider is disabled or not configured")

    async def validate_model(self) -> tuple[bool, str]:
        return False, "LLM provider disabled"

    async def aclose(self) -> None:
        return None
