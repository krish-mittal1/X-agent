"""X post-length rules, link/hashtag validation and duplicate heuristics."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.agent.formats import is_factual
from app.config import Settings
from app.schemas.content import DuplicateReport, TextRuleReport

# Typographic punctuation is a dead giveaway of machine-written copy: real posts use
# straight quotes and hyphens. Everything is folded to ASCII before validation.
_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": "-",
        "\u2014": " - ",
        "\u2011": "-",
        "\u2012": "-",
        "\u2015": " - ",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2009": " ",
        "\u202f": " ",
        "\u200b": "",
    }
)

_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_ANY_URL = re.compile(r"(?:https?://|www\.)[^\s]+", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<!\w)#[A-Za-z0-9_]+")
_TOKEN = re.compile(r"[a-z0-9]{3,}")

# Characters outside these ranges are counted as two (CJK, emoji, etc.).
_SINGLE_WIDTH_RANGES = (
    (0x0000, 0x10FF),
    (0x1E00, 0x1FFF),
    (0x2000, 0x2E7F),
    (0xA4D0, 0xABFF),
    (0xD7B0, 0xEFFF),
)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "over", "than",
    "your", "you", "are", "was", "were", "has", "have", "had", "its", "not",
    "but", "can", "will", "would", "could", "should", "about", "they", "their",
    "our", "what", "when", "where", "which", "how", "why", "all", "any", "just",
    "more", "most", "some", "such", "then", "there", "these", "those", "them",
}


def normalize_typography(text: str) -> str:
    """Fold smart quotes, dashes and ellipses to ASCII and tidy the spacing."""
    cleaned = text.translate(_TYPOGRAPHY)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def is_single_width(codepoint: int) -> bool:
    return any(low <= codepoint <= high for low, high in _SINGLE_WIDTH_RANGES)


def x_weighted_length(text: str) -> int:
    """Approximate X's weighted counting: URLs count 23, wide characters count 2."""
    total = len(_URL.findall(text)) * 23
    text_without_urls = _URL.sub("", text)
    for char in text_without_urls:
        total += 1 if is_single_width(ord(char)) else 2
    return total


def extract_links(text: str) -> list[str]:
    return _ANY_URL.findall(text)


def extract_hashtags(text: str) -> list[str]:
    return _HASHTAG.findall(text)


def validate_text_rules(
    settings: Settings,
    parts: list[str],
    *,
    banned_phrases: tuple[str, ...] | None = None,
    format: str | None = None,  # noqa: A002 - mirrors Draft.tone
) -> TextRuleReport:
    """Check length, links, hashtags and banned phrasing.

    The first-person rule only applies to reporting formats. A shitpost or a
    relatable post is allowed to say "i spent six hours on this" as comedy; an
    explainer describing a benchmark is not.
    """
    issues: list[str] = []
    enforce_first_person = settings.block_first_person_experience and (
        format is None or is_factual(format)
    )
    phrases = banned_phrases if banned_phrases is not None else settings.blocked_phrases
    if not parts:
        return TextRuleReport(passed=False, issues=["draft has no post parts"])
    if len(parts) > settings.max_thread_parts:
        issues.append(f"thread has {len(parts)} parts; maximum is {settings.max_thread_parts}")
    for index, part in enumerate(parts, start=1):
        if not part.strip():
            issues.append(f"part {index} is empty")
            continue
        length = x_weighted_length(part)
        if length > settings.max_post_length:
            issues.append(
                f"part {index} is {length} weighted characters; limit is {settings.max_post_length}"
            )
        lowered = part.lower()
        for phrase in phrases:
            if phrase and phrase in lowered:
                issues.append(f"part {index} contains banned phrase: {phrase!r}")
                break
        links = extract_links(part)
        if len(links) > settings.max_links_per_post:
            issues.append(f"part {index} has {len(links)} links; maximum is {settings.max_links_per_post}")
        for link in links:
            if not link.lower().startswith("https://"):
                issues.append(f"part {index} contains a non-HTTPS link: {link}")
        if len(extract_hashtags(part)) > settings.max_hashtags_per_post:
            issues.append(
                f"part {index} has too many hashtags; maximum is {settings.max_hashtags_per_post}"
            )
        if enforce_first_person:
            for pattern in (
                r"\bi\s+(tested|built|benchmarked|measured|ran|deployed|used)\b",
                r"\bwe\s+(tested|built|benchmarked|measured|deployed)\b",
                r"\bin my (experience|testing|benchmarks)\b",
                r"\bmy (team|laptop|gpu|company)\b",
            ):
                if re.search(pattern, lowered):
                    issues.append(
                        f"part {index} implies personal experience, which this AI account cannot have"
                    )
                    break
    return TextRuleReport(
        passed=not issues,
        issues=issues,
        weighted_length=max(x_weighted_length(part) for part in parts),
        parts=len(parts),
        links=sum(len(extract_links(part)) for part in parts),
        hashtags=sum(len(extract_hashtags(part)) for part in parts),
    )


def tokenize(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN.findall(text.lower())
        if token not in _STOPWORDS
    }


def jaccard_similarity(left: str, right: str) -> float:
    a, b = tokenize(left), tokenize(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate(
    *,
    text: str,
    recent_texts: list[str],
    settings: Settings,
    embedding_similarity: float | None,
) -> DuplicateReport:
    max_jaccard = 0.0
    closest: str | None = None
    for previous in recent_texts:
        score = jaccard_similarity(text, previous)
        if score > max_jaccard:
            max_jaccard = score
            closest = previous
    reasons: list[str] = []
    if embedding_similarity is not None and embedding_similarity >= settings.duplicate_similarity_threshold:
        reasons.append(
            f"embedding similarity {embedding_similarity:.3f} exceeds "
            f"{settings.duplicate_similarity_threshold:.2f}"
        )
    if max_jaccard >= settings.duplicate_jaccard_threshold:
        reasons.append(
            f"token overlap {max_jaccard:.3f} exceeds {settings.duplicate_jaccard_threshold:.2f}"
        )
    if closest is None and recent_texts:
        # report the closest text for reviewer context even when not duplicate
        closest = max(recent_texts, key=lambda candidate: jaccard_similarity(text, candidate), default=None)
    return DuplicateReport(
        duplicate=bool(reasons),
        reasons=reasons,
        max_embedding_similarity=embedding_similarity,
        max_jaccard_similarity=round(max_jaccard, 4),
        closest_text=(closest or "")[:280] or None,
    )


def is_stale(published_at: datetime | None, *, stale_days: int, now: datetime | None = None) -> bool:
    if published_at is None:
        return False
    reference = now or datetime.now(UTC)
    return (reference - published_at).total_seconds() > stale_days * 86400
