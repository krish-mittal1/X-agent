"""Deterministic prompt-injection heuristics for untrusted content.

These checks do not make the LLM safe on their own. They complement the prompt
boundaries and the application policy gates: a source that tries to instruct the
model is flagged at ingestion time and can never be used as evidence, even if a
model repeats its text verbatim.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)\b", re.I),
    re.compile(r"\bdisregard\s+(the\s+)?(system|previous|prior)\b", re.I),
    re.compile(r"\bsystem\s+(override|prompt|message)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(a|an|the)\b", re.I),
    re.compile(r"\b(reveal|print|show)\s+(your|the)\s+(system\s+)?(prompt|instructions)\b", re.I),
    re.compile(r"\b(publish|post|tweet)\s+(this\s+)?immediately\b", re.I),
    re.compile(r"\bnew\s+instructions\s*:", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
]


def detect_prompt_injection(text: str) -> list[str]:
    """Return the matched injection markers (empty when nothing suspicious found)."""
    if not text:
        return []
    matches: list[str] = []
    for pattern in _PATTERNS:
        found = pattern.search(text)
        if found:
            matches.append(found.group(0)[:80])
    return matches
