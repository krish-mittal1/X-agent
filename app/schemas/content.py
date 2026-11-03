"""Schemas for drafting, verification and duplicate checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClaimType = Literal["fact", "interpretation", "estimate", "opinion"]


class DraftClaim(BaseModel):
    claim: str = Field(min_length=3, max_length=500)
    claim_type: ClaimType
    supporting_quote: str = Field(
        default="",
        max_length=800,
        description="Exact text copied from one supplied source that supports a factual claim.",
    )
    source_url: str = Field(default="", max_length=1000)


class DraftContent(BaseModel):
    """Structured LLM output for one original draft."""

    topic: str = Field(min_length=3, max_length=200)
    parts: list[str] = Field(min_length=1, max_length=10)
    rationale: str = Field(min_length=1, max_length=1200)
    claims: list[DraftClaim] = Field(default_factory=list, max_length=20)
    uncertainty: list[str] = Field(default_factory=list, max_length=8)


class ReplyDraftContent(BaseModel):
    """Structured LLM output for a draft reply (never sent automatically)."""

    parts: list[str] = Field(min_length=1, max_length=2)
    rationale: str = Field(min_length=1, max_length=1200)
    claims: list[DraftClaim] = Field(default_factory=list, max_length=10)
    send_recommended: bool = True
    uncertainty: list[str] = Field(default_factory=list, max_length=5)


class ClaimCheck(BaseModel):
    claim: str
    claim_type: ClaimType
    status: Literal["verified", "unverified", "contradicted", "stale", "not_applicable"]
    verdict: Literal["supported", "partially_supported", "contradicted", "unresolved", "outdated", "not_applicable"] = "not_applicable"
    claim_category: str = "factual"  # numerical, financial, political, quote, prediction, factual
    source_document_id: int | None = None
    source_url: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    notes: str = ""
    independent_sources_count: int = 0
    primary_source_verified: bool = False
    evidence_quote: str = ""


class VerificationReport(BaseModel):
    passed: bool
    claims: list[ClaimCheck] = Field(default_factory=list)
    verified_facts: int = 0
    unverified_facts: int = 0
    contradicted_claims: int = 0
    unresolved_claims: int = 0
    partially_supported_claims: int = 0
    independent_sources_used: int = 0
    hard_stop_triggered: bool = False
    hard_stop_reasons: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_at: str = ""


class TextRuleReport(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    weighted_length: int = 0
    parts: int = 0
    links: int = 0
    hashtags: int = 0


class DuplicateReport(BaseModel):
    duplicate: bool
    reasons: list[str] = Field(default_factory=list)
    max_embedding_similarity: float | None = None
    max_jaccard_similarity: float = 0.0
    closest_text: str | None = None


class ContentGenerationError(RuntimeError):
    """Draft could not be generated (provider unavailable, invalid output)."""


class DraftOutcome(BaseModel):
    draft_id: int
    status: str
    blocked_reasons: list[str] = Field(default_factory=list)
    verification: VerificationReport
    text_rules: TextRuleReport
    duplicate: DuplicateReport
