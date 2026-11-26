"""Originality gate: does the post add something of our own?

Reposting a wire headline unmodified is duplicate content at machine speed,
which is what hard-caps reach on a small account. This module checks the draft
text against the source headline/excerpt without calling a model:

* the opening line must not simply *be* the headline;
* the opening must contribute enough words that are not in the headline;
* no long verbatim run may be lifted from the source body.

The caller decides what to do with a failure. Nothing here mutates a draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Settings
from app.publishing.text_rules import tokenize

_WORD = re.compile(r"[a-z0-9']+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]")


@dataclass(frozen=True)
class OriginalityReport:
    passed: bool
    headline_overlap: float
    commentary_words: int
    verbatim_words: int
    opening: str
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "headline_overlap": round(self.headline_overlap, 3),
            "commentary_words": self.commentary_words,
            "verbatim_words": self.verbatim_words,
            "opening": self.opening[:180],
            "issues": list(self.issues),
        }


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _longest_common_run(left: list[str], right: list[str]) -> int:
    """Length (in words) of the longest shared contiguous run.

    Above roughly a dozen words this stops looking like agreement and starts
    looking like a copy-paste, so it is the signal we measure.
    """
    if not left or not right:
        return 0
    best = 0
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, start=1):
            if token == other:
                current[index] = previous[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        previous = current
    return best


def _opening(text: str) -> str:
    """First paragraph, else first sentence: the part people read as our voice."""
    stripped = text.strip()
    if not stripped:
        return ""
    paragraph = next(
        (block.strip() for block in re.split(r"\n\s*\n", stripped) if block.strip()),
        "",
    )
    if paragraph and paragraph != stripped:
        return paragraph
    match = _SENTENCE_END.search(stripped)
    if match:
        return stripped[: match.end()].strip()
    return stripped


def assess_originality(
    *,
    text: str,
    settings: Settings,
    source_headline: str = "",
    source_excerpt: str = "",
    original_post_available: bool = False,
    is_operator: bool = False,
) -> OriginalityReport:
    """Judge whether ``text`` carries original commentary over the source.

    ``original_post_available`` marks a draft built from our own reporting (no
    external headline to copy), and ``is_operator`` marks hand-written drafts;
    both are exempt from the headline-copy rules.
    """
    body = text or ""
    opening = _opening(body)
    issues: list[str] = []

    headline = (source_headline or "").strip()
    excerpt = (source_excerpt or "").strip()

    if not opening:
        return OriginalityReport(
            passed=False,
            headline_overlap=0.0,
            commentary_words=0,
            verbatim_words=0,
            opening="",
            issues=("post is empty",),
        )

    if not is_operator:
        lowered_opening = opening.casefold()
        repeated_prefix = next(
            (phrase for phrase in settings.generic_opening_phrases
             if lowered_opening.startswith(phrase.casefold())),
            None,
        )
        if repeated_prefix:
            issues.append(
                f"generic opening template '{repeated_prefix}'; lead with a specific detail"
            )

    headline_tokens = tokenize(headline) if headline else set()
    opening_tokens = tokenize(opening)
    overlap = 0.0
    if headline_tokens and opening_tokens:
        overlap = len(opening_tokens & headline_tokens) / len(opening_tokens)

    commentary_tokens = opening_tokens - headline_tokens if headline_tokens else opening_tokens
    commentary_words = len(commentary_tokens)

    verbatim = 0
    if excerpt:
        verbatim = _longest_common_run(_words(body), _words(excerpt))

    min_commentary = max(0, int(settings.growth_min_commentary_words))
    max_overlap = float(settings.growth_max_headline_overlap)
    max_verbatim = max(0, int(settings.growth_max_verbatim_words))

    if not is_operator and not original_post_available:
        if headline_tokens and overlap >= max_overlap:
            issues.append(
                "opening line is the source headline "
                f"({overlap:.0%} of its words); lead with your own take"
            )
        elif commentary_words < min_commentary:
            issues.append(
                f"only {commentary_words} word(s) of commentary beyond the headline; "
                f"minimum is {min_commentary}"
            )
        if verbatim >= max_verbatim:
            issues.append(
                f"{verbatim} words copied verbatim from the source; rewrite in our own words"
            )

    return OriginalityReport(
        passed=not issues,
        headline_overlap=overlap,
        commentary_words=commentary_words,
        verbatim_words=verbatim,
        opening=opening,
        issues=tuple(issues),
    )
