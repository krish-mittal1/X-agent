from __future__ import annotations

import pytest

from app.providers.rate_limit import (
    RateLimitExceededError,
    SlidingWindowLimiter,
    estimate_tokens,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_limiter(**overrides: object) -> tuple[SlidingWindowLimiter, FakeClock]:
    clock = FakeClock()
    params: dict[str, object] = {
        "requests_per_minute": 30,
        "tokens_per_minute": 1000,
        "requests_per_day": 50,
        "tokens_per_day": 100_000,
        "safety_ratio": 1.0,
        "sleep": _sleep_for(clock),
    }
    params.update(overrides)
    params["clock"] = clock
    return SlidingWindowLimiter(**params), clock  # type: ignore[arg-type]


def _sleep_for(clock: FakeClock):  # type: ignore[no-untyped-def]
    async def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    return sleeper


async def test_requests_are_recorded_and_reported() -> None:
    limiter, _ = make_limiter()
    reservation = await limiter.acquire(100)
    assert reservation is not None
    reservation.settle(120)
    snapshot = limiter.snapshot()
    assert snapshot["minute_requests"] == 1
    assert snapshot["minute_tokens"] == 120


async def test_settle_replaces_the_estimate() -> None:
    limiter, _ = make_limiter()
    reservation = await limiter.acquire(900)
    assert limiter.snapshot()["minute_tokens"] == 900
    reservation.settle(80)
    assert limiter.snapshot()["minute_tokens"] == 80


async def test_settle_is_idempotent() -> None:
    limiter, _ = make_limiter()
    reservation = await limiter.acquire(500)
    reservation.settle(100)
    reservation.settle(100)
    assert limiter.snapshot()["minute_tokens"] == 100


async def test_waits_instead_of_exceeding_tokens_per_minute() -> None:
    limiter, clock = make_limiter(tokens_per_minute=1000)
    await limiter.acquire(600)
    await limiter.acquire(600)  # would cross 1000, so it must wait out the window
    assert clock.now >= 60.0
    assert limiter.total_waited_seconds >= 60.0
    # After the wait the first entry has aged out, so we are back under the ceiling.
    assert limiter.snapshot()["minute_tokens"] <= 1000


async def test_waits_instead_of_exceeding_requests_per_minute() -> None:
    limiter, clock = make_limiter(requests_per_minute=2, tokens_per_minute=100_000)
    await limiter.acquire(1)
    await limiter.acquire(1)
    await limiter.acquire(1)
    assert clock.now >= 60.0


async def test_daily_request_quota_raises_and_stops_work() -> None:
    limiter, _ = make_limiter(requests_per_day=2, tokens_per_minute=100_000)
    await limiter.acquire(1)
    await limiter.acquire(1)
    with pytest.raises(RateLimitExceededError, match="daily request quota"):
        await limiter.acquire(1)


async def test_daily_token_quota_raises() -> None:
    limiter, _ = make_limiter(
        requests_per_day=1000, tokens_per_minute=100_000, tokens_per_day=500
    )
    await limiter.acquire(400)
    with pytest.raises(RateLimitExceededError, match="daily token quota"):
        await limiter.acquire(200)


async def test_request_larger_than_the_minute_ceiling_fails_fast() -> None:
    limiter, clock = make_limiter(tokens_per_minute=1000)
    with pytest.raises(RateLimitExceededError, match="per-minute ceiling"):
        await limiter.acquire(5000)
    # It must not burn the whole budget waiting for something impossible.
    assert clock.now == 0.0


async def test_safety_ratio_shrinks_the_effective_ceiling() -> None:
    limiter, _ = make_limiter(tokens_per_minute=1000, safety_ratio=0.5)
    assert limiter.effective_tokens_per_minute == 500
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire(600)


async def test_disabled_limiter_never_blocks() -> None:
    limiter, clock = make_limiter(enabled=False, tokens_per_minute=10)
    for _ in range(20):
        assert await limiter.acquire(1000) is None
    assert clock.now == 0.0


def test_estimate_tokens_is_proportional_and_never_zero() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 350) == 100
    assert estimate_tokens("a" * 350, chars_per_token=0) == 100
