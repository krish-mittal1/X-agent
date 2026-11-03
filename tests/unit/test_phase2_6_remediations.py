from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.analytics.performance import PerformanceAnalyticsService
from app.config import Settings
from app.main import create_app
from app.memory.models import ResearchSource
from app.research.security import is_safe_url, validate_safe_url
from app.research.verification import ClaimVerifier, SourceView
from app.schemas.content import DraftClaim

# ==============================================================================
# Phase G: SSRF and URL Security Tests
# ==============================================================================

def test_ssrf_protection_blocks_private_and_metadata_destinations():
    blocked_targets = [
        "http://localhost:8000/api",
        "http://127.0.0.1:8000/ready",
        "http://127.0.1.1",
        "http://10.0.0.5/admin",
        "http://192.168.1.100",
        "http://172.16.0.2",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/passwd",
        "ftp://internal.server/data",
        "javascript:alert(1)",
    ]
    for target in blocked_targets:
        assert not is_safe_url(target), f"Target should be blocked: {target}"
        with pytest.raises(ValueError, match="Blocked unsafe"):
            validate_safe_url(target)

def test_ssrf_protection_allows_valid_external_https():
    allowed_targets = [
        "https://news.google.com/rss",
        "https://pib.gov.in/PressReleasePage.aspx?PRID=123",
        "https://www.thehindu.com/news/national/",
        "https://economictimes.indiatimes.com/tech",
    ]
    for target in allowed_targets:
        assert is_safe_url(target), f"Target should be allowed: {target}"
        assert validate_safe_url(target) == target

# ==============================================================================
# Phase F: Analytics Integrity (No Fabricated Metrics) Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_analytics_never_fabricates_metrics_when_no_data():
    mock_session = AsyncMock()
    # Mock post query returning empty list
    mock_posts_res = MagicMock()
    mock_posts_res.all.return_value = []
    
    # Mock evals query returning 0 evals
    mock_evals_res = MagicMock()
    mock_evals_res.first.return_value = (0, 0)
    
    mock_session.execute.side_effect = [mock_posts_res, mock_evals_res]

    service = PerformanceAnalyticsService(mock_session)
    report = await service.generate_report(account_id=1, days=30)

    assert report.total_posts_analyzed == 0
    assert report.total_impressions == 0
    assert report.total_engagements == 0
    assert report.avg_engagement_rate == 0.0
    assert report.critic_approval_rate == 0.0  # NOT 88.0
    assert report.insufficient_data is True
    assert len(report.insights) == 0  # No fabricated observations!

# ==============================================================================
# Phase C: Multi-Source Claim Verification Tests
# ==============================================================================

def test_multi_source_corroboration_and_syndication():
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
    )
    verifier = ClaimVerifier(settings)

    # Source 1 (Candidate doc)
    src1 = ResearchSource(
        id=1,
        url="https://source1.com/news/story",
        title="Source 1 Headline",
        publisher="Source One",
        content_excerpt="The government approved the revised policy today.",
        syndication_hash="hash_alpha",
    )

    # Source 2 (Independent source supporting claim)
    src2 = ResearchSource(
        id=2,
        url="https://thehindu.com/news/story",
        title="The Hindu Story",
        publisher="The Hindu",
        content_excerpt="Tata Electronics invested ₹91,000 crore in the Dholera semiconductor fab.",
        syndication_hash="hash_beta",
    )

    # Source 3 (Independent corroborator for Source 2 claim)
    src3 = ResearchSource(
        id=3,
        url="https://business-standard.com/markets",
        title="Business Standard Report",
        publisher="Business Standard",
        content_excerpt="According to ministry filings, Tata Electronics invested ₹91,000 crore in the Dholera semiconductor fab.",
        syndication_hash="hash_gamma",
    )

    # Source 4 (Syndicated copy of Source 2)
    src4 = ResearchSource(
        id=4,
        url="https://syndicate-clone.com/story",
        title="Syndicated Mirror",
        publisher="Syndicate Clone",
        content_excerpt="Tata Electronics invested ₹91,000 crore in the Dholera semiconductor fab.",
        syndication_hash="hash_beta",  # Identical syndication hash to Source 2!
    )

    # Claim supported by Source 2
    claim_tata = DraftClaim(
        claim="Tata Electronics invested ₹91,000 crore in Dholera.",
        claim_type="fact",
        supporting_quote="Tata Electronics invested ₹91,000 crore in the Dholera semiconductor fab.",
        source_url=src2.url,
    )

    # When verified against multi-source evidence (src1, src2, src3, src4):
    report = verifier.verify([claim_tata], [src1, src2, src3, src4])
    assert report.passed is True
    assert report.verified_facts == 1
    assert report.unverified_facts == 0

    # Test independent source counting:
    # src2 and src4 share hash_beta, while src3 has hash_gamma.
    # Therefore independent count should be 2, NOT 3.
    views = [SourceView(s) for s in [src1, src2, src3, src4]]
    indep_count = verifier._count_independent_sources(claim_tata.supporting_quote, views)
    assert indep_count == 2, f"Expected 2 independent sources, got {indep_count}"

def test_contradicted_claim_triggers_hard_stop():
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
    )
    verifier = ClaimVerifier(settings)

    src = ResearchSource(
        id=1,
        url="https://source.com/story",
        title="Initial Report",
        publisher="Source",
        content_excerpt="The project was completed on June 15.",
    )

    claim = DraftClaim(
        claim="The project was completed on June 15.",
        claim_type="fact",
        supporting_quote="The project was completed on June 15.",
    )

    conflicting_info = [
        {
            "claim": "The project was completed on June 15.",
            "source_a": "https://source.com/story",
            "source_b": "https://reuters.com/dispute",
            "contradiction_detail": "Reuters reports the project remained stalled and incomplete until August.",
        }
    ]

    report = verifier.verify([claim], [src], conflicting_claims=conflicting_info)
    assert report.passed is False
    assert report.hard_stop_triggered is True
    assert report.contradicted_claims == 1
    assert any("contradiction" in reason.lower() for reason in report.hard_stop_reasons)

# ==============================================================================
# Phase B: API Authentication and Tenant Isolation Tests
# ==============================================================================

def test_editorial_endpoints_require_admin_auth():
    # Give this auth test an explicit token. Without one, the intended
    # fail-closed behaviour is 503 (admin API disabled), which is covered by
    # the integration health test.
    app = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            admin_api_token=SecretStr("test-admin-token"),
            x_publishing_mode="local_draft",
            publishing_enabled=False,
            dry_run=True,
        )
    )
    client = TestClient(app)

    # 1. Unauthenticated requests must be rejected with 401 or 403
    r_eval = client.get("/api/editorial/evaluations/1")
    assert r_eval.status_code in (401, 403), f"Expected 401/403 unauthenticated, got {r_eval.status_code}"

    r_switch = client.post("/api/editorial/switch-angle", json={"draft_id": 1, "target_angle": "contrarian"})
    assert r_switch.status_code in (401, 403), f"Expected 401/403 unauthenticated, got {r_switch.status_code}"

    r_insights = client.get("/api/analytics/performance/insights")
    assert r_insights.status_code in (401, 403), f"Expected 401/403 unauthenticated, got {r_insights.status_code}"

    # 2. Invalid credentials must be rejected with 401 or 403
    bad_headers = {"Authorization": "Bearer totally_fake_and_invalid_token"}
    r_eval_bad = client.get("/api/editorial/evaluations/1", headers=bad_headers)
    assert r_eval_bad.status_code in (401, 403)

    r_switch_bad = client.post(
        "/api/editorial/switch-angle",
        json={"draft_id": 1, "target_angle": "contrarian"},
        headers=bad_headers,
    )
    assert r_switch_bad.status_code in (401, 403)

@pytest.mark.asyncio
async def test_switch_draft_angle_enforces_safety_and_validation():
    from fastapi import HTTPException

    from app.api.editorial_routes import SwitchAngleRequest, switch_draft_angle
    from app.memory.models import Account, Draft

    # 1. Reject if target angle is not in alternative angles
    mock_session = AsyncMock()
    account = Account(id=1, handle="ssh_bugs", is_active=True)
    mock_session.scalar.return_value = account
    draft = Draft(
        id=10,
        account_id=1,
        body="Original draft text",
        tone="analytical",
        policy_report={"alternative_angles": [{"angle": "contrarian", "parts": ["Alternative contrarian body text"]}]},
    )
    mock_session.get.return_value = draft

    # Request invalid angle
    req = SwitchAngleRequest(draft_id=10, target_angle="nonexistent_angle")
    settings = Settings(x_publishing_mode="local_draft")
    
    with pytest.raises(HTTPException) as exc_info:
        await switch_draft_angle(req, "admin", mock_session, settings)
    assert exc_info.value.status_code == 400
    assert "not found in alternative angles" in str(exc_info.value.detail)

    # 2. Reject if draft belongs to another account (Tenant Isolation)
    draft_other = Draft(
        id=20,
        account_id=2,  # Belongs to account 2!
        body="Other account draft",
        tone="analytical",
        policy_report={"alternative_angles": [{"angle": "contrarian", "parts": ["text"]}]},
    )
    mock_session.get.return_value = draft_other
    with pytest.raises(HTTPException) as exc_info:
        await switch_draft_angle(SwitchAngleRequest(draft_id=20, target_angle="contrarian"), "admin", mock_session, settings)
    assert exc_info.value.status_code == 404
