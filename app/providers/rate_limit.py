"""Client-side pacing for provider rate limits.

Gemini 2.5 Flash free tier allows 15 requests/minute and 1,000,000 tokens/minute (verified from the ``x-ratelimit-*`` response headers).
The limit that actually bites is tokens/minute: one structured draft request
carries the persona, the JSON schema and the source excerpts, so a repair retry
can cross 8000 tokens on its own.

This limiter makes the client wait *before* sending instead of discovering the
limit from a 429. It is deliberately conservative: the effective token ceiling is
``llm_rate_limit_safety_ratio`` of the configured limit, because usage is
estimated from character counts and the real tokenizer can disagree.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.logging_config import get_logger

logger = get_logger(__name__)

_MINUTE = 60.0
_DAY = 86_400.0
_MAX_WAIT_CYCLES = 8


class RateLimitExceededError(RuntimeError):
    """A quota cannot be satisfied by waiting (daily cap or oversized request)."""


@dataclass
class _Reservation:
    """A booked request whose token estimate is corrected once usage is known."""

    entry: list[float] = field(default_factory=list)
    estimated_tokens: int = 0
    settled: bool = False

    def settle(self, actual_tokens: int) -> int:
        """Replace the estimate with real usage. Idempotent."""
        if self.settled or not self.entry:
            return 0
        self.settled = True
        delta = max(0, actual_tokens) - self.estimated_tokens
        self.entry[1] = float(max(0, actual_tokens))
        return delta


class SlidingWindowLimiter:
    """Tracks requests and tokens over the last minute and the last day."""

    def __init__(
        self,
        *,
        requests_per_minute: int,
        tokens_per_minute: int,
        requests_per_day: int,
        tokens_per_day: int,
        safety_ratio: float = 0.9,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.requests_per_minute = max(1, requests_per_minute)
        self.tokens_per_minute = max(1, tokens_per_minute)
        self.requests_per_day = max(1, requests_per_day)
        self.tokens_per_day = max(1, tokens_per_day)
        ratio = min(1.0, max(0.1, safety_ratio))
        self.effective_tokens_per_minute = max(1, int(self.tokens_per_minute * ratio))
        self.effective_tokens_per_day = max(1, int(self.tokens_per_day * ratio))
        self.enabled = enabled
        self.clock = clock
        self.sleep = sleep
        self._minute: deque[list[float]] = deque()
        self._day: deque[list[float]] = deque()
        self.total_waited_seconds = 0.0
        self.total_requests = 0

    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        while self._minute and now - self._minute[0][0] >= _MINUTE:
            self._minute.popleft()
        while self._day and now - self._day[0][0] >= _DAY:
            self._day.popleft()

    def snapshot(self) -> dict[str, float | int]:
        now = self.clock()
        self._prune(now)
        return {
            "minute_requests": len(self._minute),
            "minute_tokens": int(sum(entry[1] for entry in self._minute)),
            "day_requests": len(self._day),
            "day_tokens": int(sum(entry[1] for entry in self._day)),
            "limit_rpm": self.requests_per_minute,
            "limit_tpm": self.tokens_per_minute,
            "limit_rpd": self.requests_per_day,
            "limit_tpd": self.tokens_per_day,
            "waited_seconds": round(self.total_waited_seconds, 1),
        }

    def _wait_for_token_headroom(self, now: float, estimated: int) -> float:
        """Seconds until ``estimated`` more tokens fit under the minute ceiling."""
        used = sum(entry[1] for entry in self._minute)
        excess = used + estimated - self.effective_tokens_per_minute
        if excess <= 0:
            return 0.0
        freed = 0.0
        for timestamp, tokens in self._minute:
            freed += tokens
            if freed >= excess:
                return max(0.0, _MINUTE - (now - timestamp)) + 0.05
        # Everything in the window must expire before this request fits.
        return _MINUTE + 0.05

    # ------------------------------------------------------------------
    async def acquire(self, estimated_tokens: int) -> _Reservation | None:
        """Wait until a request of this size is allowed, then book it."""
        if not self.enabled:
            return None
        estimated = max(0, int(estimated_tokens))

        if estimated > self.effective_tokens_per_minute:
            raise RateLimitExceededError(
                f"request needs ~{estimated} tokens but the per-minute ceiling is "
                f"{self.effective_tokens_per_minute}; shorten the prompt "
                "(lower DRAFT_SOURCE_CONTEXT_CHARS) or raise LLM_TOKENS_PER_MINUTE"
            )

        for _ in range(_MAX_WAIT_CYCLES):
            now = self.clock()
            self._prune(now)

            if len(self._day) >= self.requests_per_day:
                raise RateLimitExceededError(
                    f"daily request quota reached ({self.requests_per_day}/day); "
                    "queued work will run after the quota resets"
                )
            day_tokens = sum(entry[1] for entry in self._day)
            if day_tokens + estimated > self.effective_tokens_per_day:
                raise RateLimitExceededError(
                    f"daily token quota reached ({self.effective_tokens_per_day} "
                    f"of {self.tokens_per_day}/day); stopping for today"
                )

            wait = 0.0
            if len(self._minute) >= self.requests_per_minute:
                oldest = self._minute[0][0]
                wait = max(wait, max(0.0, _MINUTE - (now - oldest)) + 0.05)
            wait = max(wait, self._wait_for_token_headroom(now, estimated))

            if wait <= 0:
                entry = [now, float(estimated)]
                self._minute.append(entry)
                self._day.append(entry)
                self.total_requests += 1
                return _Reservation(entry=entry, estimated_tokens=estimated)

            logger.info(
                "pacing LLM request to stay under rate limit",
                extra={
                    "extra_fields": {
                        "wait_seconds": round(wait, 2),
                        "estimated_tokens": estimated,
                        "minute_requests": len(self._minute),
                        "minute_tokens": int(sum(item[1] for item in self._minute)),
                    }
                },
            )
            self.total_waited_seconds += wait
            await self.sleep(wait)

        raise RateLimitExceededError(
            "could not fit the request under the rate limit after repeated waits"
        )


def estimate_tokens(text: str, chars_per_token: float = 3.5) -> int:
    """Rough token estimate; only used for pre-request pacing."""
    if chars_per_token <= 0:
        chars_per_token = 3.5
    return max(1, int(len(text) / chars_per_token))
