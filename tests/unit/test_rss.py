from __future__ import annotations

from datetime import UTC

import httpx

from app.config import Settings
from app.ingestion.extract import extract_article_image, is_valid_article_image
from app.ingestion.rss import RssAdapter, _entry_image

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Engineering</title>
  <item>
    <title>New inference engine cuts latency</title>
    <link>https://example.com/blog/inference?utm_source=rss</link>
    <pubDate>Tue, 27 Oct 2026 10:00:00 GMT</pubDate>
    <description><![CDATA[<p>Short summary.</p>]]></description>
    <guid>post-1</guid>
  </item>
  <item>
    <title>Internal admin link</title>
    <link>http://127.0.0.1/secret</link>
    <description>should be rejected</description>
  </item>
</channel></rss>"""

ARTICLE = """<html><head><title>New inference engine cuts latency</title></head><body>
<article><p>The engine processes 1,200 tokens per second on a single CPU core, according to the
project's benchmark notes. It replaces the previous scheduler and adds a paged KV cache.</p>
<p>Migration requires version 2.4 or newer.</p></article></body></html>"""


def make_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "_env_file": None,
        "rss_feeds": "https://example.com/feed",
        "http_timeout_seconds": 5.0,
        "per_host_min_interval_seconds": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


async def test_rss_adapter_fetches_feed_and_article(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def no_dns_check(host: str) -> None:
        return None

    monkeypatch.setattr("app.net.assert_host_is_public", no_dns_check)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})
        if request.url.path == "/blog/inference":
            return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
        return httpx.Response(404, text="missing")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = RssAdapter(make_settings(), client)
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()

    assert len(result.items) == 1
    item = result.items[0]
    assert item.url == "https://example.com/blog/inference"
    assert item.metadata["article_fetched"] is True
    assert "paged KV cache" in item.content
    assert item.published_at is not None and item.published_at.tzinfo == UTC
    assert item.external_id == "post-1"
    assert item.reliability_score > 0.5
    assert result.rejected == 1  # loopback entry rejected


async def test_rss_adapter_falls_back_to_summary_when_article_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def no_dns_check(host: str) -> None:
        return None

    monkeypatch.setattr("app.net.assert_host_is_public", no_dns_check)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(200, text=FEED)
        return httpx.Response(500, text="broken")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = RssAdapter(make_settings(), client)
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()

    assert len(result.items) == 1
    assert result.items[0].fetch_status == "summary_only"
    assert "Short summary" in result.items[0].content
    assert result.items[0].fetch_error


async def test_rss_adapter_reports_malformed_feed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def no_dns_check(host: str) -> None:
        return None

    monkeypatch.setattr("app.net.assert_host_is_public", no_dns_check)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>not a feed</body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = RssAdapter(make_settings(), client)
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()
    assert result.items == []
    assert any("malformed" in error for error in result.errors)


async def test_rss_adapter_rejects_unsafe_feed_urls() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    adapter = RssAdapter(make_settings(), client, feed_urls=("http://127.0.0.1/feed",))
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()
    assert result.items == []
    assert result.rejected == 1
    assert any("rejected feed" in error for error in result.errors)


def test_is_valid_article_image() -> None:
    assert is_valid_article_image("https://example.com/photos/parliament_clash.jpg") is True
    assert is_valid_article_image("https://example.com/photos/scam_bust.webp") is True
    assert is_valid_article_image("https://example.com/photos/train_derail.png") is True

    # Rejection of logos, icons, avatars, tracking pixels
    assert is_valid_article_image("https://example.com/site-logo.png") is False
    assert is_valid_article_image("https://example.com/favicon.ico") is False
    assert is_valid_article_image("https://example.com/assets/default_avatar.jpg") is False
    assert is_valid_article_image("https://example.com/tracking/1x1.gif") is False
    assert is_valid_article_image("https://example.com/icon.svg") is False
    assert is_valid_article_image("") is False
    assert is_valid_article_image(None) is False


def test_extract_article_image_opengraph() -> None:
    html_og = """
    <html><head>
    <meta property="og:image" content="https://img.news.com/2026/11/sitharaman_speech.jpg" />
    <meta name="twitter:image" content="https://img.news.com/2026/11/sitharaman_speech_tw.jpg" />
    </head><body><article>Budget row details</article></body></html>
    """
    assert extract_article_image(html_og) == "https://img.news.com/2026/11/sitharaman_speech.jpg"

    # Fallback to twitter:image if og:image missing
    html_tw = """
    <html><head>
    <meta name="twitter:image" content="https://img.news.com/2026/11/ed_raids.jpg" />
    </head><body><article>ED raids</article></body></html>
    """
    assert extract_article_image(html_tw) == "https://img.news.com/2026/11/ed_raids.jpg"


def test_entry_image_from_enclosures_and_media() -> None:
    # 1. media_content
    entry1 = {"media_content": [{"url": "https://img.news.com/photo1.jpg"}]}
    assert _entry_image(entry1) == "https://img.news.com/photo1.jpg"

    # 2. enclosures
    entry2 = {"enclosures": [{"href": "https://img.news.com/photo2.webp"}]}
    assert _entry_image(entry2) == "https://img.news.com/photo2.webp"

    # 3. HTML img tag in summary
    entry3 = {"summary": "<p>Breaking news <img src='https://img.news.com/photo3.jpg' /> details</p>"}
    assert _entry_image(entry3) == "https://img.news.com/photo3.jpg"

    # 4. Skip logos
    entry_logo = {"media_content": [{"url": "https://img.news.com/brand_logo.png"}]}
    assert _entry_image(entry_logo) is None
