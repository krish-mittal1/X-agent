from __future__ import annotations

import httpx
import pytest

from app.net import (
    RetryPolicy,
    UnsafeURLError,
    assert_host_is_public,
    backoff_delay,
    content_hash,
    normalize_url,
    retry_async,
    safe_get,
    validate_url_shape,
)


def test_normalize_url_strips_tracking_and_fragment() -> None:
    url = "HTTPS://Example.COM:443/Post/?utm_source=x&b=2&a=1&ref=hn#section"
    assert normalize_url(url) == "https://example.com/Post/?a=1&b=2"


def test_normalize_url_keeps_non_default_port() -> None:
    assert normalize_url("http://example.com:8080/feed/") == "http://example.com:8080/feed/"


def test_normalize_url_preserves_trailing_slash_verbatim() -> None:
    """The slash is part of the path; stripping it caused redirect loops."""
    assert normalize_url("https://example.com/feed/") == "https://example.com/feed/"
    assert normalize_url("https://example.com/feed") == "https://example.com/feed"
    assert normalize_url("https://example.com/") == "https://example.com/"
    # normalisation must be idempotent, or re-validating a redirect target loops
    once = normalize_url("https://example.com/feed/")
    assert normalize_url(once) == once


def test_normalize_url_rejects_credentials_and_bad_schemes() -> None:
    with pytest.raises(UnsafeURLError):
        normalize_url("https://user:pass@example.com/")
    with pytest.raises(UnsafeURLError):
        normalize_url("file:///etc/passwd")
    with pytest.raises(UnsafeURLError):
        normalize_url("javascript:alert(1)")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/feed",
        "http://127.0.0.1/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://example.com:22/",
        "http://metadata.google.internal/",
    ],
)
def test_validate_url_shape_blocks_ssrf_targets(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_url_shape(url)


def test_validate_url_shape_allows_public_urls() -> None:
    assert validate_url_shape("https://github.blog/feed") == "https://github.blog/feed"
    assert validate_url_shape("http://8.8.8.8/") == "http://8.8.8.8/"


async def test_assert_host_is_public_checks_ip_literals() -> None:
    with pytest.raises(UnsafeURLError):
        await assert_host_is_public("192.168.1.10")
    await assert_host_is_public("8.8.8.8")


def test_content_hash_ignores_whitespace_and_case() -> None:
    assert content_hash("Hello   World") == content_hash("hello world\n")


def test_backoff_is_bounded() -> None:
    policy = RetryPolicy(attempts=5, base_delay=1.0, max_delay=4.0, jitter=0.5)
    delays = [backoff_delay(policy, attempt) for attempt in range(1, 8)]
    assert all(0 <= d <= 4.0 for d in delays)
    assert delays[3] >= delays[0] * 0.5


async def test_retry_async_stops_after_attempts_and_reports_retries() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []
    retries: list[tuple[int, str]] = []

    async def failing() -> str:
        calls["n"] += 1
        raise TimeoutError("boom")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(TimeoutError):
        await retry_async(
            failing,
            RetryPolicy(attempts=3, base_delay=0.1, jitter=0),
            retry_on=(TimeoutError,),
            on_retry=lambda attempt, exc, delay: retries.append((attempt, str(exc))),
            sleep=fake_sleep,
        )
    assert calls["n"] == 3
    assert len(sleeps) == 2
    assert retries[0][0] == 1


async def test_retry_async_returns_first_success() -> None:
    calls = {"n": 0}

    async def flaky() -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("nope")
        return 42

    async def no_sleep(_: float) -> None:
        return None

    result = await retry_async(
        flaky,
        RetryPolicy(attempts=4, base_delay=0),
        retry_on=(ConnectionError,),
        sleep=no_sleep,
    )
    assert result == 42
    assert calls["n"] == 3


async def test_safe_get_survives_slash_redirect_without_looping() -> None:
    """Regression: normalisation once stripped the trailing slash, so the 301
    target collapsed back onto the URL just fetched and safe_get gave up with
    'too many redirects' instead of ever fetching the feed."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/feed":
            return httpx.Response(301, headers={"location": "/feed/"})
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            content=b"<rss version=\"2.0\"><channel><title>t</title></channel></rss>",
        )

    # A public IP literal keeps this offline: assert_host_is_public resolves only
    # hostnames, and MockTransport intercepts the request before any socket.
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        page = await safe_get(
            "http://93.184.216.34/feed",
            client=client,
            timeout_seconds=5,
            max_bytes=10_000,
            max_redirects=3,
        )
    finally:
        await client.aclose()

    assert page.status_code == 200
    assert page.final_url == "http://93.184.216.34/feed/"
    # exactly one hop: no request is ever repeated
    assert seen == ["/feed", "/feed/"]


async def test_safe_get_fetches_slash_url_directly() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            content=b"<rss version=\"2.0\"><channel/></rss>",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        page = await safe_get(
            "http://93.184.216.34/feed/",
            client=client,
            timeout_seconds=5,
            max_bytes=10_000,
            max_redirects=3,
        )
    finally:
        await client.aclose()

    assert page.status_code == 200
    assert seen == ["/feed/"]
