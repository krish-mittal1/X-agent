"""RSS/Atom ingestion with full-article retrieval and SSRF protection."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from app.config import Settings
from app.ingestion.base import AdapterResult, SourceAdapterError
from app.ingestion.extract import (
    clean_excerpt,
    extract_article_image,
    extract_text,
    is_valid_article_image,
)
from app.ingestion.source_registry import classify_source
from app.logging_config import get_logger
from app.net import (
    FetchError,
    PerHostRateLimiter,
    UnsafeURLError,
    normalize_url,
    safe_get,
    validate_url_shape,
)
from app.schemas.research import IngestedItem

logger = get_logger(__name__)

MAX_CONTENT_CHARS = 30_000


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
            except (OverflowError, ValueError):
                continue
    return None


def _entry_html(entry: Any) -> str:
    contents = entry.get("content") or []
    if contents and isinstance(contents, list):
        best = max(contents, key=lambda item: len(item.get("value", "")))
        if best.get("value"):
            return str(best["value"])
    return str(entry.get("summary") or entry.get("description") or "")


def _entry_image(entry: Any) -> str | None:
    """Extract editorial photo directly from RSS enclosures or media tags."""
    # 1. media_content or media_thumbnail
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        if isinstance(items, list):
            for item in items:
                url = item.get("url") if isinstance(item, dict) else None
                if is_valid_article_image(url):
                    return url
    # 2. enclosures
    for enc in entry.get("enclosures") or []:
        url = enc.get("href") or enc.get("url")
        if is_valid_article_image(url):
            return url
    # 3. HTML img tag in summary/description/content
    html = _entry_html(entry)
    m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
    if m and is_valid_article_image(m.group(1)):
        return m.group(1)
    return None


def parse_feed(text: str) -> list[dict[str, Any]]:
    """Parse RSS/Atom text into lightweight entries. Never performs network I/O."""
    lowered = text[:4000].lower()
    looks_like_feed = any(marker in lowered for marker in ("<rss", "<feed", "<rdf:rdf", "<rdf"))
    parsed = feedparser.parse(text)
    entries = list(parsed.get("entries") or [])
    if entries:
        return entries
    if parsed.get("bozo"):
        raise SourceAdapterError(
            f"malformed feed: {parsed.get('bozo_exception', 'unknown error')}"
        )
    if not looks_like_feed:
        raise SourceAdapterError("malformed feed: response is not an RSS/Atom document")
    return []


class RssAdapter:
    name = "rss"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        feed_urls: tuple[str, ...] | None = None,
        rate_limiter: PerHostRateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.feed_urls = feed_urls if feed_urls is not None else settings.feed_urls
        self.rate_limiter = rate_limiter or PerHostRateLimiter(
            settings.per_host_min_interval_seconds
        )

    async def fetch(self) -> AdapterResult:
        result = AdapterResult()
        article_budget = self.settings.max_article_fetches_per_run
        for feed_url in self.feed_urls:
            try:
                normalized_feed = validate_url_shape(feed_url)
            except UnsafeURLError as exc:
                result.errors.append(f"rejected feed {feed_url}: {exc}")
                result.rejected += 1
                continue
            try:
                page = await safe_get(
                    normalized_feed,
                    client=self.client,
                    timeout_seconds=self.settings.http_timeout_seconds,
                    max_bytes=self.settings.source_max_bytes,
                    rate_limiter=self.rate_limiter,
                )
            except (FetchError, UnsafeURLError, httpx.HTTPError) as exc:
                result.errors.append(f"feed fetch failed {feed_url}: {exc}")
                continue
            try:
                entries = parse_feed(page.text)
            except SourceAdapterError as exc:
                result.errors.append(f"{feed_url}: {exc}")
                continue

            feed_title = ""
            parsed_meta = feedparser.parse(page.text).get("feed", {})
            feed_title = str(parsed_meta.get("title", "") or "")

            for entry in entries:
                item = await self._build_item(
                    entry, normalized_feed, feed_title, article_budget
                )
                if item is None:
                    result.rejected += 1
                    continue
                if isinstance(item, IngestedItem):
                    result.items.append(item)
                    if item.metadata.get("article_fetched"):
                        article_budget = max(0, article_budget - 1)
                else:  # rejected URL detail
                    result.errors.append(str(item))
                    result.rejected += 1
        return result

    async def _build_item(
        self,
        entry: dict[str, Any],
        feed_url: str,
        feed_title: str,
        article_budget: int,
    ) -> IngestedItem | str | None:
        raw_link = entry.get("link") or ""
        if not raw_link:
            links = entry.get("links") or []
            raw_link = links[0].get("href", "") if links else ""
        if not raw_link:
            return None
        try:
            url = normalize_url(raw_link)
            validate_url_shape(url)
        except (UnsafeURLError, ValueError) as exc:
            return f"rejected entry {raw_link!r}: {exc}"

        title = str(entry.get("title", "") or "").strip()
        raw_html = _entry_html(entry)
        summary_text, _ = extract_text(raw_html)
        content = summary_text
        image_url = _entry_image(entry)
        article_fetched = False
        fetch_status = "summary_only"
        fetch_error: str | None = None

        if article_budget > 0 and len(summary_text) < 1200:
            try:
                page = await safe_get(
                    url,
                    client=self.client,
                    timeout_seconds=self.settings.http_timeout_seconds,
                    max_bytes=self.settings.source_max_bytes,
                    rate_limiter=self.rate_limiter,
                )
                text, page_title = extract_text(page.text)
                if len(text) > len(summary_text):
                    content = text
                    article_fetched = True
                    fetch_status = "ok"
                if not title and page_title:
                    title = page_title
                if not image_url:
                    image_url = extract_article_image(page.text)
            except (FetchError, UnsafeURLError, httpx.HTTPError) as exc:
                fetch_error = f"{type(exc).__name__}: {exc}"[:300]
                fetch_status = "summary_only"

        if not content.strip():
            return None
        if len(content.strip()) >= 20 and fetch_status == "summary_only":
            fetch_status = "ok"
        profile = classify_source("rss", url)
        content = content[:MAX_CONTENT_CHARS]
        published = _entry_datetime(entry)
        author = entry.get("author")
        metadata = {
            "feed_url": feed_url,
            "feed_title": feed_title,
            "article_fetched": article_fetched,
            "word_count": len(content.split()),
            "is_primary_source": profile.is_primary,
        }
        if image_url:
            metadata["image_url"] = image_url

        return IngestedItem(
            source_type="rss",
            source_identifier=feed_url,
            url=url,
            title=title or "(untitled)",
            content=content,
            excerpt=clean_excerpt(summary_text or content),
            author=str(author)[:256] if author else None,
            published_at=published,
            reliability_score=profile.reliability,
            external_id=str(entry.get("id") or entry.get("guid") or "") or None,
            fetch_status=fetch_status,
            fetch_error=fetch_error,
            metadata=metadata,
        )
