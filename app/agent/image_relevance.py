"""Image attachment relevance and provenance checks for India Pulse drafts.

A post with no image is preferred over a misleading, generic, or unverified photo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

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


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def assess_image_attachment(
    *,
    image_url: str | None,
    source_url: str | None = None,
    topic: str = "",
    source_title: str = "",
    claimed_verified: bool = False,
    claimed_credit: str | None = None,
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

    lowered = url.lower()
    if any(hint in lowered for hint in _UNRELATED_HINTS):
        return ImageAttachmentDecision(
            attach=False,
            image_url=None,
            source_url=source_url,
            relevant=False,
            verified=False,
            reason="generic_illustration",
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
    )


def select_document_image(
    documents: Sequence[object],
    topic: str,
) -> ImageAttachmentDecision:
    """Select the first verified editorial article image from the story's source documents."""
    if not documents:
        return assess_image_attachment(image_url=None, topic=topic)

    for doc in documents:
        metadata = getattr(doc, "metadata_", None) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        img_url = metadata.get("image_url") if isinstance(metadata.get("image_url"), str) else None
        if not img_url:
            continue
        decision = assess_image_attachment(
            image_url=img_url,
            source_url=getattr(doc, "url", None),
            topic=topic,
            source_title=getattr(doc, "title", "") or "",
            claimed_verified=bool(metadata.get("image_verified")),
            claimed_credit=metadata.get("image_credit") if isinstance(metadata.get("image_credit"), str) else None,
        )
        if decision.attach and decision.image_url:
            return decision

    return assess_image_attachment(image_url=None, topic=topic)
