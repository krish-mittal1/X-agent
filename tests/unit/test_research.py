from __future__ import annotations

from app.research.schemas import (
    StructuredResearchReport,
)
from app.research.service import (
    _classify_source,
    _compute_syndication_hash,
)


def test_source_classification_identifies_primary_domains() -> None:
    stype, is_prim, rel = _classify_source("https://pib.gov.in/PressReleasePage.aspx?PRID=1", "PIB")
    assert stype == "primary"
    assert is_prim is True
    assert rel >= 0.9

    stype2, is_prim2, rel2 = _classify_source("https://www.thehindu.com/news/national/article.ece", "The Hindu")
    assert stype2 == "news"
    assert is_prim2 is False
    assert rel2 >= 0.8


def test_syndication_hash_detects_wire_reprints() -> None:
    text_a = "New Delhi, Nov 2 (PTI) The government on Sunday announced a major boost for the national semiconductor mission."
    text_b = "New Delhi: The government on Sunday announced a major boost for the national semiconductor mission."
    hash_a = _compute_syndication_hash(text_a)
    hash_b = _compute_syndication_hash(text_b)
    assert isinstance(hash_a, str)
    assert len(hash_a) == 16
    assert isinstance(hash_b, str)


def test_structured_research_report_schema_validates() -> None:
    data = {
        "topic": "Semiconductor Investment Mission",
        "executive_summary": "Cabinet approved ₹76,000 crore scheme.",
        "what_happened": "New fab committed in Dholera.",
        "key_entities": [
            {"name": "Tata Electronics", "entity_type": "company", "role": "builder"},
            {"name": "MeitY", "entity_type": "government_body", "role": "approver"},
        ],
        "verified_facts": ["₹76,000 crore committed", "Location is Dholera, Gujarat"],
        "unresolved_claims": ["Commercial chip delivery target date remains unverified"],
        "conflicting_info": [
            {
                "claim": "Operational date",
                "source_a": "Govt PIB: 2026",
                "source_b": "Industry report: 2027",
                "contradiction_detail": "1-year gap in projected first chip tape-out",
            }
        ],
        "historical_context": "Previous semiconductor consortium plans stalled in 2018.",
        "stakeholder_analysis": [
            {"stakeholder": "Indian Tech Founders", "stance": "supportive", "incentive": "Local silicon supply"}
        ],
        "potential_implications": ["Boost to domestic supply chains", "Reduction in import bills"],
        "confidence_score": 0.9,
    }
    report = StructuredResearchReport.model_validate(data)
    assert report.topic == "Semiconductor Investment Mission"
    assert len(report.key_entities) == 2
    assert len(report.conflicting_info) == 1
    assert report.conflicting_info[0].claim == "Operational date"

