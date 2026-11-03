"""Content-format selection and News Format Router.

Classifies incoming stories into appropriate news formats conforming to the
India Pulse digital news desk:
- Breaking News (significant verified events)
- Straight News (concise reporting without forced hot takes)
- Political News (attributed statements, institutional decisions)
- Geopolitics and Defence (Indian strategic relevance)
- Business and Economy (accurate figures with units)
- Quote-Led Posts (prominent figure statements with context)
- Video and Viral Updates (verified visuals)
- Explainers (why it matters to India)
- Human Interest, Entertainment and Sports (conversational cadence)
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from app.config import Settings

# Newsroom Format Constants
NEWS_FORMAT_BREAKING = "breaking_news"
NEWS_FORMAT_STRAIGHT = "straight_news"
NEWS_FORMAT_POLITICAL = "political_news"
NEWS_FORMAT_GEOPOLITICS = "geopolitics_defence"
NEWS_FORMAT_BUSINESS = "business_economy"
NEWS_FORMAT_QUOTE = "quote_led"
NEWS_FORMAT_VIDEO = "video_viral"
NEWS_FORMAT_EXPLAINER = "explainer"
NEWS_FORMAT_HUMAN_INTEREST = "human_interest_sports"

NEWS_FORMATS = (
    NEWS_FORMAT_BREAKING,
    NEWS_FORMAT_STRAIGHT,
    NEWS_FORMAT_POLITICAL,
    NEWS_FORMAT_GEOPOLITICS,
    NEWS_FORMAT_BUSINESS,
    NEWS_FORMAT_QUOTE,
    NEWS_FORMAT_VIDEO,
    NEWS_FORMAT_EXPLAINER,
    NEWS_FORMAT_HUMAN_INTEREST,
)

# Formats whose factual claims must be grounded in retrieved sources.
FACTUAL_FORMATS = frozenset({
    "analysis",
    "context",
    "explainer",
    "news_jab",
    NEWS_FORMAT_BREAKING,
    NEWS_FORMAT_STRAIGHT,
    NEWS_FORMAT_POLITICAL,
    NEWS_FORMAT_GEOPOLITICS,
    NEWS_FORMAT_BUSINESS,
    NEWS_FORMAT_QUOTE,
    NEWS_FORMAT_VIDEO,
    NEWS_FORMAT_EXPLAINER,
})

# Formats with conversational latitude
COMEDIC_FORMATS = frozenset({
    "breaking_jab",
    "opinion",
    "hot_take",
    "relatable",
    "shitpost",
    NEWS_FORMAT_HUMAN_INTEREST,
})

ALL_FORMATS = FACTUAL_FORMATS | COMEDIC_FORMATS

# Formats requiring declared numeric evidence
NUMERIC_EVIDENCE_FORMATS = frozenset({
    "analysis",
    "context",
    "explainer",
    "breaking_jab",
    "news_jab",
    "hot_take",
    NEWS_FORMAT_BUSINESS,
    NEWS_FORMAT_STRAIGHT,
    NEWS_FORMAT_BREAKING,
})

DEFAULT_FORMAT_WEIGHTS: dict[str, float] = {
    NEWS_FORMAT_STRAIGHT: 0.30,
    NEWS_FORMAT_POLITICAL: 0.18,
    NEWS_FORMAT_BUSINESS: 0.16,
    NEWS_FORMAT_EXPLAINER: 0.12,
    NEWS_FORMAT_GEOPOLITICS: 0.10,
    NEWS_FORMAT_QUOTE: 0.08,
    NEWS_FORMAT_BREAKING: 0.06,
}

_RECENT_PENALTY = 0.15


@dataclass(frozen=True)
class FormatChoice:
    format: str
    weights: dict[str, float]


def classify_news_format(
    title: str,
    content: str = "",
    metadata: dict[str, object] | None = None,
) -> str:
    """Classify a story into the most appropriate Indian newsroom format."""
    meta = metadata or {}
    text = f"{title} {content}".lower()

    # Video check
    if meta.get("has_video") or re.search(r"\b(?:watch|video|cctv|visuals)\b", title.lower()):
        return NEWS_FORMAT_VIDEO

    # Quote-led check (exact quote marks or statement verbs in title)
    if ('"' in title or "“" in title) and any(
        verb in text for verb in ["says", "said", "stated", "slams", "remarks", "declared", "told"]
    ):
        return NEWS_FORMAT_QUOTE

    # Breaking news check (major immediate developments)
    if any(
        k in text
        for k in [
            "breaking:",
            "just in:",
            "big update",
            "massive fire",
            "crash",
            "landslide",
            "earthquake",
            "killed in",
        ]
    ):
        return NEWS_FORMAT_BREAKING

    # Political news
    if any(
        k in text
        for k in [
            "parliament",
            "lok sabha",
            "rajya sabha",
            "election",
            "bjp",
            "congress",
            "aap",
            "supreme court",
            "high court",
            "minister",
            "cabinet",
            "cm ",
            "governor",
            "alleged",
            "alleges",
        ]
    ):
        return NEWS_FORMAT_POLITICAL

    # Geopolitics & Defence
    if any(
        k in text
        for k in [
            "isro",
            "drdo",
            "defence",
            "border",
            "lac",
            "loc",
            "pakistan",
            "missile",
            "navy",
            "army",
            "air force",
            "bilateral",
            "diplomacy",
            "treaty",
        ]
    ):
        return NEWS_FORMAT_GEOPOLITICS

    # Business & Economy
    if any(
        k in text
        for k in [
            "₹",
            "crore",
            "lakh",
            "sensex",
            "nifty",
            "rbi",
            "sebi",
            "gdp",
            "inflation",
            "startup",
            "ipo",
            "acquisition",
            "discom",
            "tcs",
            "infosys",
            "reliance",
            "tata",
        ]
    ):
        return NEWS_FORMAT_BUSINESS

    # Explainer check
    if any(
        k in title.lower()
        for k in ["why", "how", "explained", "what is", "here is what", "what it means"]
    ):
        return NEWS_FORMAT_EXPLAINER

    # Sports & Entertainment
    if any(
        k in text
        for k in [
            "cricket",
            "bcci",
            "ipl",
            "test match",
            "cinema",
            "box office",
            "actor",
            "actress",
            "tournament",
        ]
    ):
        return NEWS_FORMAT_HUMAN_INTEREST

    # Default to straight news reporting
    return NEWS_FORMAT_STRAIGHT


def parse_mix(raw: str) -> dict[str, float]:
    """Parse format mix string into normalized weights."""
    parsed: dict[str, float] = {}
    for chunk in raw.replace(";", ",").split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        name, _, value = item.partition(":")
        name = name.strip().lower()
        if name not in ALL_FORMATS:
            continue
        try:
            weight = float(value)
        except ValueError:
            continue
        if weight <= 0:
            continue
        parsed[name] = weight
    if not parsed:
        return dict(DEFAULT_FORMAT_WEIGHTS)
    total = sum(parsed.values())
    return {name: weight / total for name, weight in parsed.items()}


def choose_format(
    settings: Settings,
    recent_formats: list[str] | None = None,
    *,
    rng: random.Random | None = None,
) -> FormatChoice:
    """Pick a format by weight, penalising recently used ones."""
    weights = parse_mix(settings.post_format_mix)
    recent = [fmt for fmt in (recent_formats or []) if fmt in weights]
    if recent and len(weights) > 1:
        weights = dict(weights)
        weights[recent[0]] *= _RECENT_PENALTY
        total = sum(weights.values())
        if total <= 0:
            weights = parse_mix(settings.post_format_mix)
        else:
            weights = {name: weight / total for name, weight in weights.items()}

    generator = rng or random.Random()
    names = sorted(weights)
    pick = generator.choices(names, weights=[weights[name] for name in names], k=1)[0]
    return FormatChoice(format=pick, weights=weights)


def is_factual(fmt: str) -> bool:
    return fmt in FACTUAL_FORMATS


def is_comedic(fmt: str) -> bool:
    return fmt in COMEDIC_FORMATS


def requires_numeric_evidence(fmt: str) -> bool:
    return fmt in NUMERIC_EVIDENCE_FORMATS


def normalise_format(value: str | None) -> str:
    """Map a stored/tone value onto a known format, defaulting to straight_news."""
    candidate = (value or "").strip().lower()
    if candidate in ALL_FORMATS:
        return candidate
    return NEWS_FORMAT_STRAIGHT
