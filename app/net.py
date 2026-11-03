"""Network helpers: URL normalisation, SSRF guards, bounded retries, safe fetching.

Every outbound research request goes through :func:`safe_get`, which:
  * allows only http/https,
  * rejects credentials, local hostnames and private/link-local/metadata IPs,
  * validates every redirect hop,
  * enforces a byte cap and a timeout,
  * applies per-host rate limiting.

A DNS-rebinding TOCTOU window remains between validation and connection; this is
documented in README (trusted source allowlist is the primary mitigation).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "ref",
    "ref_src",
    "source",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")
_ALLOWED_PORTS = {80, 443, 8080, 8443}


class UnsafeURLError(ValueError):
    """Raised when a URL fails SSRF / scheme validation."""


class FetchError(RuntimeError):
    """Raised when a remote resource cannot be retrieved safely."""


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {url!r}") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsafeURLError(f"unsupported URL: {url!r}")
    if ":" in host:
        raise UnsafeURLError("IPv6 literal URLs are not supported for research fetches")
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    # The trailing slash is part of the path (RFC 3986) and origins routinely 301
    # between the two forms. Re-writing it here made a redirect target collapse
    # back onto the URL we had just requested, sending safe_get into an infinite
    # redirect loop (github.blog/feed/ and simonwillison.net/atom/everything/ both
    # failed this way), so it is preserved verbatim. Dedupe stays correct because
    # store_item also matches on the URL-independent content hash.
    path = parsed.path or "/"
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    query = urlencode(sorted(query_pairs))
    return urlunparse((parsed.scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_host_is_public(host: str) -> None:
    lowered = host.lower()
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"blocked host: {host}")
    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        if not _is_public_ip(lowered):
            raise UnsafeURLError(f"blocked private/reserved IP: {host}")
        return

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"DNS resolution failed for {host}") from exc
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise FetchError(f"DNS returned no addresses for {host}")
    for address in addresses:
        if not _is_public_ip(address):
            raise UnsafeURLError(f"{host} resolves to non-public address {address}")


def validate_url_shape(url: str) -> str:
    """Synchronous validation (scheme/credentials/port/hostname); DNS is checked at fetch."""
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    assert parsed.hostname is not None
    host = parsed.hostname.lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"blocked host: {host}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _is_public_ip(host):
            raise UnsafeURLError(f"blocked private/reserved IP: {host}")
    if parsed.port and parsed.port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"blocked port: {parsed.port}")
    return normalized


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25


def backoff_delay(policy: RetryPolicy, attempt: int) -> float:
    """Exponential backoff with full-range jitter, capped at max_delay."""
    raw = min(policy.max_delay, policy.base_delay * (2 ** max(0, attempt - 1)))
    return raw * (1 - policy.jitter) + random.uniform(0, raw * policy.jitter)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...],
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last_error = exc
            if attempt >= policy.attempts:
                break
            delay = backoff_delay(policy, attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await sleep(delay)
    assert last_error is not None
    raise last_error


class PerHostRateLimiter:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str, *, clock: Callable[[], float] | None = None) -> None:
        loop = asyncio.get_running_loop()
        now = clock() if clock else loop.time()
        async with self._lock:
            last = self._last.get(host, 0.0)
            delay = max(0.0, self._min_interval - (now - last))
            self._last[host] = now + delay
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str


async def safe_get(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int = 3,
    rate_limiter: PerHostRateLimiter | None = None,
) -> FetchedPage:
    current = validate_url_shape(url)
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        assert parsed.hostname is not None
        await assert_host_is_public(parsed.hostname)
        if rate_limiter is not None:
            await rate_limiter.wait(parsed.hostname)
        request = client.build_request(
            "GET",
            current,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, application/json;q=0.9, */*;q=0.5"},
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise FetchError("redirect without Location header")
                current = validate_url_shape(str(response.url.join(location)))
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} for {current}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(f"response exceeds {max_bytes} byte cap")
                chunks.append(chunk)
            body = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            try:
                text = body.decode(encoding, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return FetchedPage(
                url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                text=text,
            )
        finally:
            await response.aclose()
    raise FetchError("too many redirects")
