"""Pydantic schemas for Multi-Source Research Engine and Event Intelligence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ResearchSourceRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Topic or query to research")
    max_sources: int = Field(default=8, ge=2, le=20)


class SourceItem(BaseModel):
    url: str
    title: str
    publisher: str = "Unknown"
    published_at: datetime | None = None
    source_type: Literal["primary", "news", "regulatory", "aggregator"] = "news"
    is_primary: bool = False
    reliability_score: float = Field(default=0.7, ge=0.0, le=1.0)
    relevance_score: float = Field(default=0.8, ge=0.0, le=1.0)
    content_excerpt: str = ""
    syndication_hash: str | None = None


class ClaimItem(BaseModel):
    claim_text: str
    claim_type: Literal["numerical", "financial", "political", "factual", "statement", "prediction"] = "factual"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    context_notes: str = ""
    is_disputed: bool = False


class EntityItem(BaseModel):
    name: str
    entity_type: Literal["person", "company", "government_body", "institution", "country", "location"] = "institution"
    role: str = "subject"


class ContradictionItem(BaseModel):
    claim: str
    source_a: str
    source_b: str
    contradiction_detail: str


class StakeholderItem(BaseModel):
    stakeholder: str
    stance: str
    incentive: str


class StructuredResearchReport(BaseModel):
    topic: str
    executive_summary: str
    what_happened: str
    key_entities: list[EntityItem] = Field(default_factory=list)
    verified_facts: list[str] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)
    conflicting_info: list[ContradictionItem] = Field(default_factory=list)
    historical_context: str = ""
    stakeholder_analysis: list[StakeholderItem] = Field(default_factory=list)
    potential_implications: list[str] = Field(default_factory=list)
    confidence_score: float = Field(default=0.8, ge=0.0, le=1.0)


class EventDevelopmentItem(BaseModel):
    id: int
    title: str
    description: str
    development_date: datetime
    significance_score: float
    source_url: str | None = None
    delta_type: str


class EventSummary(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    status: str
    initial_timestamp: datetime
    latest_timestamp: datetime
    developments_count: int = 0
    entities: list[str] = Field(default_factory=list)


class EventTimelineResponse(BaseModel):
    event: EventSummary
    timeline: list[EventDevelopmentItem]
    previous_coverage: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)

