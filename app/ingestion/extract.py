"""HTML text extraction with no third-party parser dependency."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")
_SKIP_TAGS = {"script", "style", "noscript", "svg", "form", "iframe", "template"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs):  # type: ignore[no-untyped-def]
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.parts.append(data)


def extract_text(html: str) -> tuple[str, str]:
    """Return (plain_text, page_title) from raw HTML."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was collected
        pass
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines())
    text = _WHITESPACE.sub(" ", text.replace("\n\n", "\n")).strip()
    title = _WHITESPACE.sub(" ", "".join(parser.title_parts)).strip()
    return text, title


_JUNK_IMAGE_PATTERNS = re.compile(
    r"(?:favicon|logo|default_avatar|avatar|1x1|spacer|tracker|banner_ad|placeholder|site-header|nav-icon|sprite|\.ico|\.svg)",
    re.IGNORECASE,
)


def is_valid_article_image(url: str | None) -> bool:
    """Validate that an image URL points to a legitimate article photo, not a logo or icon."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    return not bool(_JUNK_IMAGE_PATTERNS.search(url))


def extract_article_image(html: str) -> str | None:
    """Extract primary editorial photo (og:image, twitter:image) from raw HTML."""
    if not html:
        return None

    # 1. OpenGraph / Twitter meta image tags
    meta_patterns = [
        re.compile(r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\'](https?://[^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']', re.IGNORECASE),
        re.compile(r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\'](https?://[^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']', re.IGNORECASE),
        re.compile(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\'](https?://[^"\']+)["\']', re.IGNORECASE),
    ]
    for pattern in meta_patterns:
        match = pattern.search(html)
        if match:
            candidate = match.group(1).strip()
            if is_valid_article_image(candidate):
                return candidate

    return None


def clean_excerpt(text: str, limit: int = 500) -> str:
    cleaned = _WHITESPACE.sub(" ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def strip_html_fragment(fragment: str) -> str:
    text, _ = extract_text(fragment)
    return text
