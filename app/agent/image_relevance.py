"""Image attachment relevance, provenance and quality checks.

The old rule was "first attachable image wins", which happily attached a
200x200 portrait thumbnail while a clean 1200x675 lead photo sat in the second
source. Selection is now a small deterministic ranking:

1. only images that pass the provenance/junk gate are candidates;
2. candidates are scored on same-source verification, landscape shape, size,
   junk filename hints, credit availability and document order;
3. identical images (ignoring CDN resize parameters) collapse to one;
4. images we already used recently are avoided while an alternative exists.

A post with no image is still better than a misleading, generic or unprovenanced
photo, so every path here can return "do not attach".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.ingestion.extract import is_valid_article_image

_UNRELATED_HINTS = (
    "stock-photo",
    "stock_photo",
    "istock",
    "shutterstock",
    "gettyimages",
    "generic",
    "illustration",
    "placeholder",
    "default-og",
    "default_og",
    "site-logo",
    "og-default",
)

# Filenames and paths that are almost never the story's photo, even when they
# pass the generic article-image check.
_LOW_QUALITY_HINTS = (
    "logo",
    "icon",
    "favicon",
    "sprite",
    "avatar",
    "author",
    "profile",
    "badge",
    "sprite-",
    "sprite_",
    "sprite.",
    "watermark",
    "spacer",
    "pixel.",
    "blank.",
    "1x1",
    "2x2",
)

_SMALL_DIMENSION_HINTS = re.compile(
    r"(?<!\d)(\d{2,4})x(\d{2,4})(?!\d)", re.IGNORECASE
)

# Query parameters that only describe a CDN resize of the same photo.
_RESIZE_PARAMS = frozenset(
    {"w", "h", "width", "height", "resize", "quality", "q", "fit", "crop", "auto", "dpr"}
)

_KNOWN_SOURCE_CDNS: dict[str, tuple[str, ...]] = {
    "thehindu.com": ("thgim.com", "thehindu.com"),
    "ndtv.com": ("ndtvimg.com", "ndtv.com"),
    "indiatimes.com": ("toiimg.com", "etimg.com", "indiatimes.com"),
    "indiatoday.in": ("tosshub.com", "indiatoday.in"),
    "hindustantimes.com": ("ht-assets.com", "hindustantimes.com"),
    "indianexpress.com": ("indianexpress.com",),
    "livemint.com": ("livemint.com",),
    "news18.com": ("news18.com", "cnbctv18.com"),
}


def is_same_source_or_cdn(image_host: str, source_host: str) -> bool:
    """Check if the image is hosted on the same origin or an authorized publisher CDN."""
    if not image_host or not source_host:
        return False
    if (
        image_host == source_host
        or image_host.endswith("." + source_host)
        or source_host.endswith("." + image_host)
    ):
        return True
    for main_domain, cdns in _KNOWN_SOURCE_CDNS.items():
        if (source_host == main_domain or source_host.endswith("." + main_domain)) and any(
            image_host == cdn or image_host.endswith("." + cdn) for cdn in cdns
        ):
            return True
    return False


@dataclass(frozen=True)
class ImageAttachmentDecision:
    attach: bool
    image_url: str | None
    source_url: str | None
    relevant: bool
    verified: bool
    reason: str
    credit: str | None = None
    width: int | None = None
    height: int | None = None
    score: float = 0.0

    @property
    def aspect_ratio(self) -> float | None:
        if self.width and self.height:
            return self.width / self.height
        return None


@dataclass(frozen=True)
class ImageCandidate:
    """One candidate image plus everything known about its provenance."""

    image_url: str
    source_url: str | None = None
    source_title: str = ""
    width: int | None = None
    height: int | None = None
    claimed_verified: bool = False
    claimed_credit: str | None = None
    document_index: int = 0


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def normalize_image_url(url: str) -> str:
    """Strip CDN resize parameters so two sizes of one photo compare equal."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _RESIZE_PARAMS
    ]
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urlencode(kept), "")
    )


def _dimensions_from_url(url: str) -> tuple[int | None, int | None]:
    match = _SMALL_DIMENSION_HINTS.search(url)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _is_low_quality_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in (*_UNRELATED_HINTS, *_LOW_QUALITY_HINTS))


def assess_image_attachment(
    *,
    image_url: str | None,
    source_url: str | None = None,
    topic: str = "",
    source_title: str = "",
    claimed_verified: bool = False,
    claimed_credit: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> ImageAttachmentDecision:
    """Decide whether an image may be attached, and whether it can be called verified.

    Never invent credits. Never label an image verified unless the URL is a real
    article image tied to the source being reported.
    """
    if not image_url or not isinstance(image_url, str):
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="no_image",
        )

    url = image_url.strip()
    if not is_valid_article_image(url):
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="invalid_or_junk_image",
        )

    if _is_low_quality_url(url):
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="generic_illustration",
        )

    if width and height and (width < 300 or height < 160):
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="image_too_small",
            width=width,
            height=height,
        )

    image_host = _host(url)
    source_host = _host(source_url)
    same_source = is_same_source_or_cdn(image_host, source_host)
    if source_url and image_host and source_host and not same_source:
        # Cross-domain photos are often unrelated politician or file photos.
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="unrelated_or_unprovenanced_image",
        )

    # Provenance is established only when the image comes from the article itself or publisher CDN.
    verified = bool(same_source and source_url)
    if claimed_verified and not verified:
        verified = False
    credit = claimed_credit.strip() if claimed_credit and claimed_credit.strip() else None
    if credit and credit.lower() in {"verified editorial photo", "staff", "file photo"}:
        credit = None

    return ImageAttachmentDecision(
        attach=True,
        image_url=url,
        source_url=source_url,
        relevant=True,
        verified=verified,
        reason="source_article_image" if verified else "unverified_but_plausible",
        credit=credit,
        width=width,
        height=height,
    )


def score_image_candidate(
    candidate: ImageCandidate, *, recent_image_urls: Sequence[str] = ()
) -> tuple[float, ImageAttachmentDecision]:
    """Score one candidate; attachable candidates always outrank rejected ones."""
    if candidate.width is None or candidate.height is None:
        url_width, url_height = _dimensions_from_url(candidate.image_url)
        width = candidate.width or url_width
        height = candidate.height or url_height
    else:
        width, height = candidate.width, candidate.height

    decision = assess_image_attachment(
        image_url=candidate.image_url,
        source_url=candidate.source_url,
        source_title=candidate.source_title,
        claimed_verified=candidate.claimed_verified,
        claimed_credit=candidate.claimed_credit,
        width=width,
        height=height,
    )
    if not decision.attach:
        return float("-inf"), decision

    score = 1.0
    if decision.verified:
        score += 2.0
    if decision.credit:
        score += 0.2

    ratio = width / height if width and height else None
    if ratio is not None:
        if ratio >= 1.5:  # 3:2 / 16:9 crop: what a timeline renders best
            score += 1.5
        elif ratio >= 1.15:
            score += 0.8
        elif ratio <= 0.85:  # portrait thumbnails crop badly in-feed
            score -= 1.0
        else:
            score -= 0.3

    if width and height:
        if width >= 1000 and height >= 500:
            score += 0.8
        elif width >= 600 and height >= 315:
            score += 0.4
        elif width < 500 or height < 250:
            score -= 2.0
    else:
        url_width, url_height = _dimensions_from_url(candidate.image_url)
        if url_width and url_height:
            score += 0.2

    score -= 0.05 * max(0, candidate.document_index)

    if recent_image_urls:
        normalized = normalize_image_url(candidate.image_url)
        if normalized in {normalize_image_url(item) for item in recent_image_urls}:
            # Reusing one photo across posts reads as filler; only used as a last resort.
            score -= 4.0

    return score, decision


def rank_image_candidates(
    candidates: Sequence[ImageCandidate], *, recent_image_urls: Sequence[str] = ()
) -> list[tuple[float, ImageCandidate, ImageAttachmentDecision]]:
    """Attachable candidates, best first, with duplicates collapsed."""
    best_by_url: dict[str, tuple[float, ImageCandidate, ImageAttachmentDecision]] = {}
    for candidate in candidates:
        score, decision = score_image_candidate(
            candidate, recent_image_urls=recent_image_urls
        )
        if not decision.attach:
            continue
        key = normalize_image_url(candidate.image_url)
        current = best_by_url.get(key)
        if current is None or score > current[0]:
            best_by_url[key] = (score, candidate, decision)
    return sorted(
        best_by_url.values(),
        key=lambda item: (-item[0], item[1].document_index),
    )


def _metadata_value(metadata: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def select_best_image(
    documents: Sequence[object],
    topic: str,
    *,
    recent_image_urls: Sequence[str] = (),
) -> ImageAttachmentDecision:
    """Pick the strongest attachable image across every source document."""
    if not documents:
        return assess_image_attachment(image_url=None, topic=topic)

    candidates: list[ImageCandidate] = []
    for index, doc in enumerate(documents):
        metadata = getattr(doc, "metadata_", None) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        image_url = metadata.get("image_url")
        if not isinstance(image_url, str) or not image_url.strip():
            continue
        candidates.append(
            ImageCandidate(
                image_url=image_url,
                source_url=getattr(doc, "url", None),
                source_title=getattr(doc, "title", "") or "",
                width=_metadata_value(metadata, "image_width", "width", "og_image_width"),
                height=_metadata_value(metadata, "image_height", "height", "og_image_height"),
                claimed_verified=bool(metadata.get("image_verified")),
                claimed_credit=(
                    metadata.get("image_credit")
                    if isinstance(metadata.get("image_credit"), str)
                    else None
                ),
                document_index=index,
            )
        )

    ranked = rank_image_candidates(candidates, recent_image_urls=recent_image_urls)
    if ranked:
        score, _candidate, decision = ranked[0]
        return ImageAttachmentDecision(
            attach=True,
            image_url=decision.image_url,
            source_url=decision.source_url,
            relevant=True,
            verified=decision.verified,
            reason=decision.reason,
            credit=decision.credit,
            width=decision.width,
            height=decision.height,
            score=round(score, 3),
        )
    return assess_image_attachment(image_url=None, topic=topic)


def select_document_image(
    documents: Sequence[object],
    topic: str,
    *,
    recent_image_urls: Sequence[str] = (),
) -> ImageAttachmentDecision:
    """Backwards-compatible entry point: best verified editorial image, or none."""
    return select_best_image(
        documents, topic, recent_image_urls=recent_image_urls
    )
