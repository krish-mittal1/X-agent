from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.memory.models import SourceDocument
from app.research.verification import (
    ClaimVerifier,
    categorize_claim,
)
from app.schemas.content import DraftClaim


def make_settings(**overrides) -> Settings:
    values = {"_env_file": None, "verification_stale_days": 14}
    values.update(overrides)
    return Settings(**values)


def test_categorize_claim() -> None:
    assert categorize_claim("Govt approves ₹76,000 crore semiconductor subsidy") == "financial"
    assert categorize_claim("Throughput improved by 40% in benchmarks") == "numerical"
    assert categorize_claim("SEBI issues new circular on derivative trading limits") == "political"
    assert categorize_claim('CEO announced: "We will ship by Q4"') == "quote"
    assert categorize_claim("Revenue is projected to reach $10B by 2030") == "financial"


def test_independent_sources_and_syndication() -> None:
    doc1 = SourceDocument(
        id=1,
        account_id=1,
        source_type="news",
        url="https://livemint.com/news/1",
        title="India Semiconductor Update",
        content="India has approved ₹76,000 crore semiconductor subsidy for fab units.",
        published_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        reliability_score=0.8,
        fetch_status="ok",
        metadata_={"syndication_hash": "ani_dispatch_123"},
    )
    # Syndicated reprint with same content
    doc2 = SourceDocument(
        id=2,
        account_id=1,
        source_type="news",
        url="https://ndtv.com/business/1",
        title="India Semiconductor Update",
        content="India has approved ₹76,000 crore semiconductor subsidy for fab units.",
        published_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        reliability_score=0.8,
        fetch_status="ok",
        metadata_={"syndication_hash": "ani_dispatch_123"},
    )

    verifier = ClaimVerifier(make_settings())
    claim = DraftClaim(
        claim="India approved ₹76,000 crore semiconductor subsidy",
        claim_type="fact",
        supporting_quote="India has approved ₹76,000 crore semiconductor subsidy",
        source_url="https://livemint.com/news/1",
    )
    report = verifier.verify([claim], [doc1, doc2])
    assert report.passed is True
    # Despite 2 documents, they share syndication hash so independent sources is counted as 1
    assert report.claims[0].independent_sources_count == 1
    assert report.claims[0].verdict == "supported"
    assert report.claims[0].claim_category == "financial"


def test_hard_stop_on_unverified_financial_claim() -> None:
    verifier = ClaimVerifier(make_settings(require_verified_claims=False))
    claim = DraftClaim(
        claim="Startup raised ₹5,000 crore in Series D",
        claim_type="fact",
        supporting_quote="Startup raised ₹5,000 crore in Series D",
        source_url="https://example.com/unrelated",
    )
    doc = SourceDocument(
        id=1,
        account_id=1,
        source_type="news",
        url="https://example.com/news",
        title="General tech news",
        content="Different story entirely with no investment info.",
        published_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        reliability_score=0.8,
        fetch_status="ok",
    )
    report = verifier.verify([claim], [doc])
    assert report.hard_stop_triggered is True
    assert report.passed is False
    assert any("Unverified quantitative assertion" in r for r in report.hard_stop_reasons)


def test_contradiction_from_research_conflicts() -> None:
    verifier = ClaimVerifier(make_settings())
    doc = SourceDocument(
        id=1,
        account_id=1,
        source_type="news",
        url="https://pib.gov.in/pressrelease",
        title="PIB release",
        content="The total sanctioned outlay is ₹76,000 crore as confirmed by the minister.",
        published_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        reliability_score=0.9,
        fetch_status="ok",
        metadata_={"is_primary_source": True},
    )
    claim = DraftClaim(
        claim="Total sanctioned outlay is ₹76,000 crore",
        claim_type="fact",
        supporting_quote="total sanctioned outlay is ₹76,000 crore",
        source_url="https://pib.gov.in/pressrelease",
    )
    conflicts = [{
        "claim": "sanctioned outlay is ₹76,000 crore",
        "contradiction_detail": "Audit report showed outlay reduced to ₹45,000 crore",
    }]
    report = verifier.verify([claim], [doc], conflicting_claims=conflicts)
    assert report.passed is False
    assert report.contradicted_claims >= 1
    assert report.claims[0].verdict == "contradicted"
    assert report.hard_stop_triggered is True

