from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.critic import EditorialCriticService
from app.agent.editorial_engine import SingleAngleCandidate
from app.agent.image_relevance import is_same_source_or_cdn
from app.agent.writer import ContentWriter, ensure_two_part_layout
from app.config import Settings
from app.memory.models import SourceDocument
from app.research.verification import ClaimVerifier
from app.schemas.content import ClaimCheck, VerificationReport


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_single_angle_candidate_two_part_assembly() -> None:
    """SingleAngleCandidate automatically merges headline_hook and context_line into parts."""
    candidate = SingleAngleCandidate(
        angle="straight_news",
        hook="Delhi's toxic air reaches the Supreme Court.",
        headline_hook="Delhi's toxic air reaches the Supreme Court.",
        context_line='CJI Sanjiv Khanna noted "the air is terrible in Delhi" as the court ordered urgent steps.',
        rationale="Top story with immediate visual impact",
        format_type="straight_news",
        claims=[],
    )
    assert len(candidate.parts) == 1
    assert candidate.parts[0] == (
        "Delhi's toxic air reaches the Supreme Court.\n\n"
        'CJI Sanjiv Khanna noted "the air is terrible in Delhi" as the court ordered urgent steps.'
    )


def test_ensure_two_part_layout_single_sentence_with_hook() -> None:
    """A single sentence post gets the champion hook prepended as headline."""
    text = "The Chief Justice agreed to an urgent hearing on Delhi's severe pollution crisis."
    hook = "Delhi air crisis reaches Supreme Court"
    result = ensure_two_part_layout(text, hook=hook)
    assert (
        result
        == "Delhi air crisis reaches Supreme Court.\n\nThe Chief Justice agreed to an urgent hearing on Delhi's severe pollution crisis."
    )


def test_ensure_two_part_layout_two_sentences_unsplit() -> None:
    """Two sentences joined by a single space are split into 2 lines with a blank line."""
    text = "Crude oil traded near $91 a barrel today. Markets reacted sharply to supply concerns following US developments."
    result = ensure_two_part_layout(text)
    assert (
        result
        == "Crude oil traded near $91 a barrel today.\n\nMarkets reacted sharply to supply concerns following US developments."
    )


def test_ensure_two_part_layout_preserves_existing_layout() -> None:
    """Already well-formatted 2-line post remains untouched."""
    text = "Oil prices rebound.\n\nCrude hit $91 following supply signals."
    result = ensure_two_part_layout(text)
    assert result == text


def test_critic_penalizes_passive_wire_leads() -> None:
    """Critic penalizes bureaucratic passive wire leads."""
    critic = EditorialCriticService()
    wire_draft = (
        "The Supreme Court has taken up a suo motu case concerning severe air pollution "
        "in the Delhi-NCR region, with the Chief Justice of India making an opening remark."
    )
    result = critic.evaluate(
        draft_text=wire_draft, topic="Delhi air pollution", angle="straight_news"
    )
    assert "passive_agency_lead" in result.flags
    assert result.naturalness_score < 75.0


def test_critic_rewards_punchy_newsdesk_post() -> None:
    """Critic rewards active 2-part structure with direct quotes and metrics."""
    critic = EditorialCriticService()
    punchy_draft = (
        "Delhi's toxic air reaches the Supreme Court.\n\n"
        '"The air is terrible in Delhi, you can\'t even breathe." — CJI Sanjiv Khanna '
        "as AQI crossed 450 across the capital."
    )
    v_report = VerificationReport(
        passed=True,
        verified_facts=2,
        unverified_facts=0,
        claims=[
            ClaimCheck(
                claim="CJI remarked air is terrible in Delhi",
                claim_type="fact",
                status="verified",
                confidence=0.9,
            ),
            ClaimCheck(
                claim="AQI crossed 450", claim_type="fact", status="verified", confidence=0.9
            ),
        ],
    )
    result = critic.evaluate(
        draft_text=punchy_draft,
        claims=[],
        verification_report=v_report,
        topic="Delhi pollution",
        angle="straight_news",
    )
    assert "passive_agency_lead" not in result.flags
    assert result.verdict == "approved"
    assert result.naturalness_score >= 80.0
    assert result.overall_score >= 80.0


def test_known_cdns_matching() -> None:
    """Legitimate Indian media publisher CDNs are accepted for provenance."""
    assert is_same_source_or_cdn("thgim.com", "thehindu.com") is True
    assert is_same_source_or_cdn("toiimg.com", "timesofindia.indiatimes.com") is True
    assert is_same_source_or_cdn("ndtvimg.com", "ndtv.com") is True
    assert is_same_source_or_cdn("tosshub.com", "indiatoday.in") is True
    assert is_same_source_or_cdn("images.indianexpress.com", "indianexpress.com") is True
    assert is_same_source_or_cdn("randomcdn.com", "thehindu.com") is False


def test_claim_verifier_synthesized_headline_tolerance() -> None:
    """Verifier permits entity-grounded synthesized headlines while enforcing numeric integrity."""
    from app.research.verification import SourceView
    from app.schemas.content import DraftClaim

    verifier = ClaimVerifier(make_settings())
    doc = SourceDocument(
        id=1,
        account_id=1,
        source_type="rss",
        source_identifier="https://thehindu.com/story",
        url="https://thehindu.com/story",
        title="Delhi Pollution Case",
        content=(
            "The Supreme Court on Thursday took up a suo motu case on severe air pollution "
            "in Delhi-NCR. Chief Justice Sanjiv Khanna remarked that air quality has worsened."
        ),
        metadata_={},
    )
    hook_claim = DraftClaim(
        claim="Delhi air pollution case taken up by Supreme Court",
        claim_type="fact",
        supporting_quote="",
        source_url="https://thehindu.com/story",
    )
    matched_doc, is_matched = verifier._match_source(hook_claim, [SourceView(doc)])
    assert is_matched is True
    assert matched_doc is not None


@pytest.mark.asyncio
async def test_jit_missing_article_image() -> None:
    """Writer fetches article webpage just-in-time when RSS metadata lacks image."""
    mock_settings = make_settings()
    mock_llm = MagicMock()
    mock_factory = MagicMock()
    writer = ContentWriter(mock_settings, mock_llm, mock_factory)

    doc = SourceDocument(
        id=1,
        account_id=1,
        source_type="rss",
        source_identifier="https://thehindu.com/news/national/story.html",
        url="https://thehindu.com/news/national/story.html",
        title="Test Story",
        content="Article content here",
        metadata_={},
    )

    fake_html = """
    <html>
      <head>
        <meta property="og:image" content="https://thgim.com/editorial/2026/photo.jpg" />
      </head>
      <body>Article text</body>
    </html>
    """

    class FakePage:
        text = fake_html

    with patch("app.net.safe_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = FakePage()
        await writer._fetch_missing_article_image([doc])

    assert doc.metadata_["image_url"] == "https://thgim.com/editorial/2026/photo.jpg"
