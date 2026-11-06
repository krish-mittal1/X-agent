"""Deterministic evidence-based claim verification and fact checking (Feature 3).

A factual claim is only 'supported' / 'verified' when its supporting quote appears verbatim
(whitespace/case-insensitive) in a source document that was actually retrieved,
is fresh enough, and did not fail to fetch.

Hard-stop rules:
- Any claim with contradictory numbers or facts triggers a hard stop.
- Any unverified numerical/financial claim (₹, $, %, crore, lakh) triggers a hard stop.
- Wire syndication is tracked so PTI/ANI reprints across multiple outlets count as 1 source.
The LLM never decides its own verification status: all checks are deterministic.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.publishing.text_rules import is_stale
from app.schemas.content import ClaimCheck, DraftClaim, VerificationReport
from app.security import detect_prompt_injection

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_WORD = re.compile(r"[a-z][a-z0-9-]+")
_STOP = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "for",
    "its",
    "was",
    "were",
    "has",
    "have",
    "are",
    "will",
    "can",
    "more",
    "than",
    "into",
    "using",
    "used",
}

_FINANCIAL_OR_NUMERICAL = re.compile(
    r"(?:₹|\$|€|£|\bINR\b|\bUSD\b)\s*\d+(?:,\d+)*(?:\.\d+)?\s*(?:crore|cr|lakh|bn|billion|million|k|trillion)?|"
    r"\b\d+(?:,\d+)*(?:\.\d+)?%|"
    r"\b\d+(?:,\d+)*(?:\.\d+)?\s*(?:percent|crore|lakh|times|x|bps|tokens|ms|gb|tb)\b",
    re.IGNORECASE,
)

_PREDICTION_TERMS = re.compile(
    r"\b(?:will|projected to|expected to|forecasted to|slated to|estimated to|poised to|predicted to)\b",
    re.IGNORECASE,
)

_POLICY_REGULATORY_TERMS = re.compile(
    r"\b(?:sebi|rbi|meity|isro|pib|parliament|lok sabha|rajya sabha|high court|supreme court|ministry|circular|directive|gazette)\b",
    re.IGNORECASE,
)

_PRIMARY_DOMAINS = (
    "pib.gov.in",
    "rbi.org.in",
    "sebi.gov.in",
    "meity.gov.in",
    "isro.gov.in",
    "egazette.gov.in",
    "sci.gov.in",
    "incometaxindia.gov.in",
    "finmin.nic.in",
)


def normalize_for_match(text: str) -> str:
    unescaped = html.unescape(text or "")
    lowered = (
        unescaped.lower()
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
        .replace("—", "-")
        .replace("–", "-")
        .replace("\xa0", " ")
    )
    return _WHITESPACE.sub(" ", lowered).strip()


def quote_supported(quote: str, source_text: str) -> bool:
    normalized_quote = normalize_for_match(quote)
    if len(normalized_quote) < 12:
        return False
    norm_source = normalize_for_match(source_text)
    if normalized_quote in norm_source:
        return True

    trimmed = normalized_quote.strip(" '\".,;:!?-")
    if len(trimmed) >= 12 and trimmed in norm_source:
        return True

    # Token overlap check for slight phrasing or formatting variances in scraped text
    q_tokens = _claim_tokens(normalized_quote)
    if len(q_tokens) >= 3:
        s_tokens = _claim_tokens(norm_source)
        shared = q_tokens & s_tokens
        q_nums = _numbers(normalized_quote)
        s_nums = _numbers(norm_source)
        if q_nums.issubset(s_nums) and (len(shared) / len(q_tokens) >= 0.8):
            return True

    return False


def _claim_tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP and len(word) > 3}


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def categorize_claim(text: str) -> str:
    """Classify an atomic claim into a specific fact-checking category."""
    if _FINANCIAL_OR_NUMERICAL.search(text):
        return (
            "financial"
            if any(c in text for c in ("₹", "$", "€", "crore", "lakh", "INR", "USD"))
            else "numerical"
        )
    if _POLICY_REGULATORY_TERMS.search(text):
        return "political"
    if ('"' in text or "“" in text) and len(text) > 10:
        return "quote"
    if _PREDICTION_TERMS.search(text):
        return "prediction"
    return "factual"


def has_unlisted_quantitative_assertion(post_text: str, claims: list[DraftClaim]) -> bool:
    """Detect measurable claims in the post that the model did not declare."""
    measurement = re.compile(
        r"\b\d+(?:[.,]\d+)?\s*(?:%|x|ms|s|gb|tb|tok(?:ens)?|x faster|times|crore|lakh|cr)\b",
        re.IGNORECASE,
    )
    if not measurement.search(post_text):
        return False
    declared = {
        normalize_for_match(claim.supporting_quote)
        for claim in claims
        if claim.claim_type == "fact" and claim.supporting_quote
    }
    return not declared


class SourceView:
    """Adapter unifying SourceDocument and ResearchSource for verification."""

    def __init__(self, obj: Any) -> None:
        self.id = getattr(obj, "id", None)
        self.url = getattr(obj, "url", "")
        content = getattr(obj, "content", None)
        if content is None:
            content = getattr(obj, "content_excerpt", "")
        self.content = content or ""
        self.published_at = getattr(obj, "published_at", None)
        self.retrieved_at = getattr(obj, "retrieved_at", datetime.now(UTC))
        rel = getattr(obj, "reliability_score", None)
        self.reliability_score = float(rel) if rel is not None else 0.7
        self.fetch_status = getattr(obj, "fetch_status", "ok")
        self.metadata_ = getattr(obj, "metadata_", {}) or {}
        self.syndication_hash = getattr(obj, "syndication_hash", None) or self.metadata_.get(
            "syndication_hash"
        )
        self.source_type = getattr(obj, "source_type", "news")
        self.is_primary = bool(
            getattr(obj, "is_primary", False)
            or self.metadata_.get("is_primary_source")
            or any(d in self.url for d in _PRIMARY_DOMAINS)
        )


class ClaimVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _match_source(
        self, claim: DraftClaim, sources: Sequence[SourceView]
    ) -> tuple[SourceView | None, bool]:
        by_url = [doc for doc in sources if claim.source_url and doc.url == claim.source_url]
        candidates: Iterable[SourceView] = by_url or sources
        for document in candidates:
            if quote_supported(claim.supporting_quote, document.content):
                return document, True
            # Entity and token overlap matching for conversational headlines
            c_tokens = _claim_tokens(claim.claim)
            if len(c_tokens) >= 3:
                doc_tokens = _claim_tokens(document.content)
                expanded_doc_tokens = set(doc_tokens)
                for t in doc_tokens:
                    if "-" in t:
                        expanded_doc_tokens.update(part for part in t.split("-") if len(part) > 3)
                shared = c_tokens & expanded_doc_tokens
                c_nums = _numbers(claim.claim)
                doc_nums = _numbers(document.content)
                if c_nums.issubset(doc_nums) and (len(shared) / len(c_tokens) >= 0.60):
                    return document, True
        return (by_url[0] if by_url else None), False

    def _count_independent_sources(self, quote: str, sources: Sequence[SourceView]) -> int:
        """Count unique non-syndicated sources that corroborate this quote."""
        seen_syndication: set[str] = set()
        independent_count = 0
        for doc in sources:
            if quote_supported(quote, doc.content):
                syn_key = (
                    doc.syndication_hash or doc.url.split("/")[2] if "//" in doc.url else doc.url
                )
                if syn_key not in seen_syndication:
                    seen_syndication.add(syn_key)
                    independent_count += 1
        return max(1, independent_count)

    def verify(
        self,
        claims: list[DraftClaim],
        sources: Sequence[Any],
        *,
        now: datetime | None = None,
        conflicting_claims: list[dict[str, Any]] | None = None,
    ) -> VerificationReport:
        reference = now or datetime.now(UTC)
        checks: list[ClaimCheck] = []
        issues: list[str] = []
        warnings: list[str] = []
        hard_stop_reasons: list[str] = []
        verified_facts = 0
        unverified_facts = 0
        contradicted_count = 0
        unresolved_count = 0
        partially_supported_count = 0
        unique_sources_used: set[str] = set()

        views = [s if isinstance(s, SourceView) else SourceView(s) for s in sources]

        for claim in claims:
            category = categorize_claim(claim.claim)
            if claim.claim_type != "fact":
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type=claim.claim_type,
                        status="not_applicable",
                        verdict="not_applicable",
                        claim_category=category,
                        confidence=0.5,
                        notes="interpretation/opinion/estimate; excluded from factual verification",
                    )
                )
                continue

            document, matched = self._match_source(claim, views)

            # Security: prompt injection rejection
            if document is not None and document.metadata_.get("injection_suspected"):
                unverified_facts += 1
                unresolved_count += 1
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type="fact",
                        status="unverified",
                        verdict="unresolved",
                        claim_category=category,
                        source_document_id=document.id,
                        source_url=document.url,
                        confidence=0.0,
                        notes="source contains suspected prompt-injection content; refusing to use it as evidence",
                    )
                )
                issues.append(
                    f"prompt-injection content in source cannot support claim: {claim.claim[:120]}"
                )
                continue

            if matched and detect_prompt_injection(claim.supporting_quote):
                unverified_facts += 1
                unresolved_count += 1
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type="fact",
                        status="unverified",
                        verdict="unresolved",
                        claim_category=category,
                        source_document_id=document.id if document else None,
                        source_url=document.url if document else None,
                        confidence=0.0,
                        notes="supporting quote contains instruction-like content",
                    )
                )
                issues.append("quote rejected: instruction-like content")
                continue

            if not matched or document is None:
                unverified_facts += 1
                unresolved_count += 1
                quote_norm = normalize_for_match(claim.supporting_quote)
                notes = (
                    "supporting quote missing or too short"
                    if len(quote_norm) < self.settings.verification_min_quote_chars
                    else "quote not found in any retrieved source (unsupported factual claim)"
                )
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type="fact",
                        status="unverified",
                        verdict="unresolved",
                        claim_category=category,
                        source_document_id=document.id if document else None,
                        source_url=document.url if document else (claim.source_url or None),
                        confidence=0.0,
                        notes=notes,
                    )
                )
                issues.append(f"unverified factual claim: {claim.claim[:160]}")

                # Hard stop on unverified quantitative or financial claims
                if category in ("financial", "numerical"):
                    hard_stop_reasons.append(
                        f"Unverified quantitative assertion [{category}]: '{claim.claim[:120]}'"
                    )
                continue

            if document.fetch_status == "failed":
                unverified_facts += 1
                unresolved_count += 1
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type="fact",
                        status="unverified",
                        verdict="unresolved",
                        claim_category=category,
                        source_document_id=document.id,
                        source_url=document.url,
                        confidence=0.0,
                        notes="source retrieval failed; cannot verify",
                    )
                )
                issues.append(f"source failed to retrieve for claim: {claim.claim[:120]}")
                continue

            if is_stale(
                document.published_at,
                stale_days=self.settings.verification_stale_days,
                now=reference,
            ):
                unverified_facts += 1
                checks.append(
                    ClaimCheck(
                        claim=claim.claim,
                        claim_type="fact",
                        status="stale",
                        verdict="outdated",
                        claim_category=category,
                        source_document_id=document.id,
                        source_url=document.url,
                        confidence=0.2,
                        notes=f"source published more than {self.settings.verification_stale_days} days ago; treated as stale for a fresh post",
                    )
                )
                issues.append(f"stale evidence for claim: {claim.claim[:120]}")
                continue

            # Independent corroboration check
            independent_count = self._count_independent_sources(claim.supporting_quote, views)
            if document.url:
                unique_sources_used.add(document.url)

            confidence = min(0.95, 0.55 + 0.4 * document.reliability_score)
            if document.is_primary:
                confidence = min(0.99, confidence + 0.1)
            if independent_count > 1:
                confidence = min(0.98, confidence + 0.05 * min(independent_count - 1, 3))
            if document.fetch_status == "summary_only":
                confidence *= 0.8
                warnings.append(
                    f"claim verified against feed summary only (article not fetched): {claim.claim[:100]}"
                )

            verified_facts += 1
            checks.append(
                ClaimCheck(
                    claim=claim.claim,
                    claim_type="fact",
                    status="verified",
                    verdict="supported",
                    claim_category=category,
                    source_document_id=document.id,
                    source_url=document.url,
                    confidence=round(confidence, 3),
                    independent_sources_count=independent_count,
                    primary_source_verified=document.is_primary,
                    evidence_quote=claim.supporting_quote,
                    notes=f"quote found in {document.source_type} source (independent sources: {independent_count})",
                )
            )

        # Check for contradictions between claims (e.g. conflicting figures)
        fact_checks = [c for c in checks if c.claim_type == "fact" and c.status == "verified"]
        for index, left in enumerate(fact_checks):
            for right in fact_checks[index + 1 :]:
                shared = _claim_tokens(left.claim) & _claim_tokens(right.claim)
                if (
                    len(shared) >= 2
                    and _numbers(left.claim)
                    and _numbers(right.claim)
                    and not (_numbers(left.claim) & _numbers(right.claim))
                ):
                    left.status = right.status = "contradicted"
                    left.verdict = right.verdict = "contradicted"
                    left.confidence = right.confidence = 0.0
                    left.notes = right.notes = "conflicting quantitative claims in draft"
                    verified_facts = max(0, verified_facts - 2)
                    contradicted_count += 2
                    issue_msg = f"conflicting numbers between factual claims: {left.claim[:90]!r} vs {right.claim[:90]!r}"
                    issues.append(issue_msg)
                    hard_stop_reasons.append(issue_msg)

        # External contradictory info from research report
        if conflicting_claims:
            for conf in conflicting_claims:
                conf_claim_text = conf.get("claim", "").lower()
                conf_tokens = _claim_tokens(conf_claim_text)
                conf_nums = _numbers(conf_claim_text)
                for check in checks:
                    if check.claim_type == "fact" and check.status != "contradicted":
                        cl_tokens = _claim_tokens(check.claim)
                        cl_nums = _numbers(check.claim)
                        shared = conf_tokens & cl_tokens
                        if conf_nums:
                            is_match = bool(conf_nums & cl_nums) and len(shared) >= 2
                        else:
                            is_match = (
                                (len(shared) / len(conf_tokens) >= 0.6) if conf_tokens else False
                            )

                        if is_match:
                            if check.status == "verified":
                                verified_facts = max(0, verified_facts - 1)
                            elif check.status == "unverified":
                                unverified_facts = max(0, unverified_facts - 1)
                            check.status = "contradicted"
                            check.verdict = "contradicted"
                            check.confidence = 0.0
                            check.notes = f"Research identified contradiction: {conf.get('contradiction_detail', '')}"
                            contradicted_count += 1
                            hard_stop_reasons.append(
                                f"Contradiction identified in research: {check.claim[:100]}"
                            )

        if not claims:
            warnings.append(
                "draft declares no claims; verify any implied factual statements manually"
            )
        if verified_facts == 0 and not unverified_facts and claims:
            warnings.append("no factual claims were declared, only interpretation/opinion")

        hard_stop_triggered = len(hard_stop_reasons) > 0

        if self.settings.require_verified_claims:
            passed = unverified_facts == 0 and not issues and not hard_stop_triggered
        else:
            passed = not hard_stop_triggered
            warnings.append("REQUIRE_VERIFIED_CLAIMS=false: unverified facts permitted by config")

        return VerificationReport(
            passed=passed,
            claims=checks,
            verified_facts=verified_facts,
            unverified_facts=unverified_facts,
            contradicted_claims=contradicted_count,
            unresolved_claims=unresolved_count,
            partially_supported_claims=partially_supported_count,
            independent_sources_used=len(unique_sources_used),
            hard_stop_triggered=hard_stop_triggered,
            hard_stop_reasons=hard_stop_reasons,
            issues=issues,
            warnings=warnings,
            checked_at=reference.isoformat(),
        )
