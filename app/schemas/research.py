"""Pydantic schemas for ingestion, scoring and research output."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class IngestedItem(BaseModel):
    """A single normalised item produced by an ingestion adapter."""

    source_type: Literal["rss", "github", "web", "x", "manual"]
    source_identifier: str
    url: str
    title: str
    content: str
    excerpt: str = ""
    author: str | None = None
    published_at: datetime | None = None
    reliability_score: float = Field(default=0.5, ge=0.0, le=1.0)
    external_id: str | None = None
    fetch_status: str = "ok"
    fetch_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateSignals(BaseModel):
    freshness: float = Field(ge=0.0, le=1.0)
    technical_relevance: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    source_reliability: float = Field(ge=0.0, le=1.0)
    evidence_quality: float = Field(ge=0.0, le=1.0)
    audience_usefulness: float = Field(ge=0.0, le=1.0)


class CandidateScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    signals: CandidateSignals
    weights: dict[str, float]
    explanation: list[str]
    llm_refinement: dict[str, Any] | None = None


class CandidateClassification(BaseModel):
    """LLM refinement of nuanced signals. It can never set source reliability."""

    technical_relevance: float = Field(ge=0.0, le=1.0)
    audience_usefulness: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=600)
    is_marketing_fluff: bool = False


class IngestionStats(BaseModel):
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = Field(default_factory=list)


class DiscoveryStats(BaseModel):
    ingested: IngestionStats = Field(default_factory=IngestionStats)
    scored: int = 0
    above_threshold: int = 0
    llm_refined: int = 0
    errors: list[str] = Field(default_factory=list)


class ClaimExtraction(BaseModel):
    """Structured claims extracted from a draft against one source document."""

    claims: list[ExtractedClaim] = Field(default_factory=list)
    notes: str = ""


class ExtractedClaim(BaseModel):
    claim_text: str = Field(min_length=3, max_length=600)
    claim_type: Literal["fact", "interpretation", "estimate", "opinion"]
    supporting_quote: str = Field(default="", max_length=800)
    source_url: str = ""


ClaimExtraction.model_rebuild()
