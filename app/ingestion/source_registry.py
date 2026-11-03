"""Source reliability heuristics and adapter registry.

Reliability values are deliberately static, human-authored heuristics. They are
never derived from LLM output or from engagement signals, so a model cannot
inflate the credibility of a claim by asserting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

# Baseline by adapter type.
SOURCE_TYPE_RELIABILITY: dict[str, float] = {
    "github": 0.85,
    "rss": 0.6,
    "web": 0.5,
    "x": 0.35,
    "manual": 0.6,
}

# Host overrides for publisher-owned primary sources.
HOST_RELIABILITY: dict[str, float] = {
    "reuters.com": 0.90,
    "apnews.com": 0.90,
    "bbc.com": 0.85,
    "bbci.co.uk": 0.85,
    "thehindu.com": 0.85,
    "foreignpolicy.com": 0.85,
    "thediplomat.com": 0.85,
    "economist.com": 0.85,
    "aljazeera.com": 0.80,
    "scmp.com": 0.80,
    "livemint.com": 0.80,
    "restofworld.org": 0.80,
    "deccanherald.com": 0.75,
    "ndtv.com": 0.75,
    "scroll.in": 0.75,
    "news.google.com": 0.80,
    "github.com": 0.85,
    "arxiv.org": 0.85,
}

PRIMARY_SOURCE_HOSTS = {
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "thehindu.com",
    "foreignpolicy.com",
    "thediplomat.com",
    "economist.com",
    "scmp.com",
    "aljazeera.com",
    "livemint.com",
    "restofworld.org",
}


@dataclass(frozen=True)
class SourceProfile:
    reliability: float
    is_primary: bool


def classify_source(source_type: str, url: str) -> SourceProfile:
    host = (urlparse(url).hostname or "").lower()
    host = host.removeprefix("www.")
    matched_host = next(
        (h for h in HOST_RELIABILITY if host == h or host.endswith("." + h)), None
    )
    reliability = (
        HOST_RELIABILITY[matched_host]
        if matched_host
        else SOURCE_TYPE_RELIABILITY.get(source_type, 0.5)
    )
    is_primary = matched_host in PRIMARY_SOURCE_HOSTS or source_type == "github"
    return SourceProfile(reliability=reliability, is_primary=is_primary)
