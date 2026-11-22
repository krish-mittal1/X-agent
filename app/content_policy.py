"""Deterministic topic boundaries shared by discovery and publishing."""

from __future__ import annotations

import re

_POLITICAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:politics|political|politician|election|electoral|manifesto|coalition|opposition)\b",
        r"\b(?:parliament|lok\s+sabha|rajya\s+sabha|vidhan\s+sabha|legislative\s+assembly)\b",
        r"\b(?:prime\s+minister|chief\s+minister|union\s+minister|foreign\s+minister|home\s+minister|cabinet|governor|government|opposition\s+leader)\b",
        r"\b(?:(?:u\.?s\.?|american|indian|russian|chinese|french|pakistani|ukrainian)\s+(?:vice\s+)?president|(?:vice\s+)?president\s+of\s+(?:india|the\s+united\s+states|russia|china|france|pakistan|ukraine))\b",
        r"\b(?:pm|cm|mp|mla|mea|nsa)\b",
        r"\b(?:bjp|bharatiya\s+janata|congress|aam\s+aadmi|shiv\s+sena|ncp|tmc|trinamool|cpi\(?m?\)?|samajwadi|rjd|janata\s+dal)\b",
        r"\b(?:narendra\s+modi|rahul\s+gandhi|amit\s+shah|arvind\s+kejriwal|mamata\s+banerjee|yogi\s+adityanath|pinarayi\s+vijayan)\b",
        r"\b(?:donald\s+trump|vladimir\s+putin|xi\s+jinping|volodymyr\s+zelenskyy?|benjamin\s+netanyahu)\b",
        r"\b(?:supreme\s+court|high\s+court|election\s+commission|ordinance|bill\s+passed)\b",
        r"\b(?:geopolitics?|diplomacy|bilateral|summit|treaty|sanctions?|ceasefire|ambassador|diplomatic\s+envoy|foreign\s+ministry|military|missile|army|navy|air\s+force|defence|border\s+conflict)\b",
        r"\b(?:war\s+of\s+words|sparks?\s+political\s+row|slams?\s+(?:the\s+)?(?:centre|government|opposition))\b",
    )
)


def is_political_content(*parts: str | None) -> bool:
    """Return true when supplied text is party/government/geopolitical content."""
    text = " ".join(part.strip() for part in parts if part and part.strip())
    return bool(text and any(pattern.search(text) for pattern in _POLITICAL_PATTERNS))


__all__ = ["is_political_content"]
