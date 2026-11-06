"""Regression coverage for the India Pulse newsroom editorial DNA."""

from __future__ import annotations

from app.agent.critic import EditorialCriticService
from app.agent.editorial_engine import EditorialCandidate
from app.agent.formats import (
    NEWS_FORMAT_BUSINESS,
    NEWS_FORMAT_POLITICAL,
    NEWS_FORMAT_QUOTE,
    classify_news_format,
)
from app.agent.image_relevance import assess_image_attachment
from app.agent.persona import DEFAULT_PERSONA


def test_default_persona_is_the_india_pulse_newsroom() -> None:
    assert DEFAULT_PERSONA.name == "india-pulse-newsdesk"
    assert DEFAULT_PERSONA.version >= 16
    assert "straight reporting" in DEFAULT_PERSONA.system_prompt.lower()


def test_news_format_router_detects_political_business_and_quote_posts() -> None:
    assert classify_news_format("Lok Sabha passes a new bill") == NEWS_FORMAT_POLITICAL
    assert classify_news_format("RBI says inflation outlook is improving") == NEWS_FORMAT_BUSINESS
    assert (
        classify_news_format('RBI Governor: "Inflation is moderating", says central bank')
        == NEWS_FORMAT_QUOTE
    )


def test_critic_requires_attribution_for_political_allegations() -> None:
    result = EditorialCriticService().evaluate(
        "The minister is corrupt and the project is a scam.",
        news_format=NEWS_FORMAT_POLITICAL,
    )
    assert "unattributed_allegation" in result.flags
    assert result.verdict == "needs_revision"


def test_critic_removes_forced_contrarian_framing() -> None:
    result = EditorialCriticService().evaluate(
        "Everyone is ignoring the real story. This exposes the uncomfortable truth.",
        angle="engaging_angle",
    )
    assert "forced_hot_take" in result.flags
    assert result.verdict == "needs_revision"


def test_thread_length_is_checked_per_post_part() -> None:
    result = EditorialCriticService().evaluate("A" * 180 + "\n\n" + "B" * 180)
    assert "length_overflow" not in result.flags


def test_candidate_preserves_news_format_separately_from_editorial_angle() -> None:
    candidate = EditorialCandidate(
        angle="context_explainer",
        news_format=NEWS_FORMAT_BUSINESS,
        hook="India's solar capacity is growing.",
        parts=["India's solar capacity is growing, with grid upgrades now a key next step."],
        rationale="Verified context.",
        claims=[],
    )
    assert candidate.to_dict()["angle"] == "context_explainer"
    assert candidate.to_dict()["news_format"] == NEWS_FORMAT_BUSINESS


def test_image_requires_source_provenance() -> None:
    decision = assess_image_attachment(
        image_url="https://cdn.unrelated.example/photo.jpg",
        source_url="https://official.example/announcement",
        topic="Railways announcement",
    )
    assert decision.attach is False
    assert decision.verified is False


def test_image_allows_verified_publisher_cdns() -> None:
    the_hindu_decision = assess_image_attachment(
        image_url="https://th-i.thgim.com/public/incoming/photo.jpg",
        source_url="https://www.thehindu.com/news/national/article.ece",
        topic="Court ruling",
    )
    assert the_hindu_decision.attach is True
    assert the_hindu_decision.verified is True
    assert the_hindu_decision.image_url == "https://th-i.thgim.com/public/incoming/photo.jpg"

    toi_decision = assess_image_attachment(
        image_url="https://static.toiimg.com/thumb/msid-12345/photo.jpg",
        source_url="https://timesofindia.indiatimes.com/india/article.cms",
        topic="Space mission",
    )
    assert toi_decision.attach is True
    assert toi_decision.verified is True


def test_ensure_two_part_layout_splits_multiple_sentences() -> None:
    from app.agent.writer import ensure_two_part_layout

    text = "First sentence here. Second sentence follows."
    formatted = ensure_two_part_layout(text)
    assert formatted == "First sentence here.\n\nSecond sentence follows."


def test_ensure_two_part_layout_uses_hook_when_single_sentence() -> None:
    from app.agent.writer import ensure_two_part_layout

    text = "The Union Home ministry has relaxed passport and visa requirements for certain foreign nationals."
    hook = "MHA eases passport norms"
    formatted = ensure_two_part_layout(text, hook=hook)
    assert formatted == f"MHA eases passport norms.\n\n{text}"
