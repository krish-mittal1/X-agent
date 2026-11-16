from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.growth.engagement import (
    EngagementService,
    extract_tweet_metadata,
    rank_viral_candidates,
)
from app.providers.base import LLMResponse, LLMUsage
from app.publishing.brave_cdp import BraveCDPPublisher


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "llm_provider": "disabled",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "rss_feeds": "",
        "github_repositories": "",
        "x_publishing_mode": "brave_cdp",
        "publishing_enabled": True,
        "dry_run": False,
        "api_expenditure_usd": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_extract_tweet_metadata() -> None:
    """Verifies parsing of author handle and tweet ID from X and Twitter URLs."""
    author, tweet_id = extract_tweet_metadata("https://x.com/ANI/status/1885544332211")
    assert author == "ANI"
    assert tweet_id == "1885544332211"

    author2, tweet_id2 = extract_tweet_metadata(
        "https://twitter.com/IndianTechGuide/status/9876543210"
    )
    assert author2 == "IndianTechGuide"
    assert tweet_id2 == "9876543210"

    author3, tweet_id3 = extract_tweet_metadata("invalid_url")
    assert author3 == ""
    assert tweet_id3 == ""


def test_viral_candidates_require_observed_threshold_and_are_ranked() -> None:
    ranked = rank_viral_candidates(
        [
            {"tweet_id": "1", "text": "small", "impressions": 999_999},
            {"tweet_id": "2", "text": "viral", "impressions": 1_500_000, "likes": 50},
            {"tweet_id": "3", "text": "bigger", "impressions": 2_000_000, "likes": 10},
            {"tweet_id": "4", "text": "unknown"},
        ],
        1_000_000,
    )

    assert [candidate["tweet_id"] for candidate in ranked] == ["3", "2"]


@pytest.mark.asyncio
async def test_generate_bangers_parses_styles() -> None:
    """Verifies that the LLM generates and parses sarcastic, value_add, and quote_tweet takes."""
    settings = make_settings()
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock()

    mock_llm.generate.return_value = LLMResponse(
        text=(
            'SARCASTIC: "Another portal where the OTP arrives 4 hours after you reached."\n'
            'VALUE_ADD: "State transport budget is down 18% YoY despite higher fuel taxes."\n'
            'QUOTE_TWEET: "Look at the timing of this announcement ahead of tomorrow\'s poll."'
        ),
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50),
        latency_ms=15.0,
        estimated_cost_usd=0.0,
        model="gemini",
    )

    mock_factory = MagicMock()
    service = EngagementService(settings, mock_llm, mock_factory)

    bangers = await service.generate_bangers(
        "Govt announces new traffic decongestion portal", target_author="ANI"
    )

    assert "OTP arrives 4 hours" in bangers["sarcastic"]
    assert "budget is down 18%" in bangers["value_add"]
    assert "Look at the timing" in bangers["quote_tweet"]


@pytest.mark.asyncio
async def test_brave_cdp_repost_simulated_in_dry_run() -> None:
    """Verifies that BraveCDP repost method returns simulated when dry_run=True."""
    settings = make_settings(dry_run=True)
    mock_factory = MagicMock()
    publisher = BraveCDPPublisher(settings, mock_factory)

    result = await publisher.repost("https://x.com/ANI/status/1885544332211")
    assert result["status"] == "simulated"
    assert result["url"] == "https://x.com/ANI/status/1885544332211"


@pytest.mark.asyncio
async def test_create_engagement_draft_stores_quoted_post_url() -> None:
    """Verifies that quote drafts store quoted_post_url in policy_report."""
    settings = make_settings()
    mock_llm = MagicMock()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add(self, obj):
            obj.id = 42

        async def flush(self):
            pass

        async def commit(self):
            pass

    mock_factory = MagicMock(return_value=FakeSession())
    service = EngagementService(settings, mock_llm, mock_factory)

    target_url = "https://x.com/IndianTechGuide/status/1885544332211"
    draft = await service.create_engagement_draft(
        account_id=1,
        target_post_url=target_url,
        content="Here is our spicy quote take on the new chip fab.",
        action="quote",
        style="value_add",
    )

    assert draft.kind == "original"
    assert draft.body == "Here is our spicy quote take on the new chip fab."
    assert draft.policy_report.get("quoted_post_url") == target_url
    assert draft.policy_report.get("target_author") == "IndianTechGuide"
    assert draft.policy_report.get("target_post_id") == "1885544332211"
    assert draft.status == "needs_review"
