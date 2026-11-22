from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.growth.engagement import (
    EngagementService,
    extract_tweet_metadata,
    rank_timely_candidates,
    rank_viral_candidates,
    rotate_handle_batch,
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


def test_handle_batches_rotate_across_the_full_pool() -> None:
    handles = ["a", "b", "c", "d", "e"]
    assert rotate_handle_batch(handles, batch_size=2, rotation_index=0) == ["a", "b"]
    assert rotate_handle_batch(handles, batch_size=2, rotation_index=1) == ["c", "d"]
    assert rotate_handle_batch(handles, batch_size=2, rotation_index=2) == ["e", "a"]


def test_timely_candidates_reject_old_pins_and_prefer_recent_reach() -> None:
    now = datetime(2026, 11, 22, tzinfo=UTC)
    def candidate(hours: int, views: int) -> dict[str, Any]:
        stamp = int((now - timedelta(hours=hours)).timestamp() * 1000)
        return {"tweet_id": str((stamp - 1288834974657) << 22), "impressions": views}
    fresh = candidate(1, 3000)
    older = candidate(10, 10000)
    assert rank_timely_candidates(
        [candidate(48, 1000000), older, fresh, {"tweet_id": "invalid"}],
        now=now, max_age_hours=12,
    ) == [fresh, older]


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


def test_viral_candidates_fallback_when_none_meet_threshold() -> None:
    """When no candidates reach the minimum threshold, fallback to ranking valid candidates."""
    candidates = [
        {"tweet_id": "10", "text": "fresh tweet 1", "impressions": 5_000, "likes": 100},
        {"tweet_id": "20", "text": "fresh tweet 2", "impressions": 8_000, "likes": 500},
        {"tweet_id": "", "text": "invalid without id", "impressions": 9_000},
    ]
    # Threshold is 50,000, but none reach it
    ranked = rank_viral_candidates(candidates, 50_000)
    assert len(ranked) == 2
    assert ranked[0]["tweet_id"] == "20"
    assert ranked[1]["tweet_id"] == "10"


def test_viral_candidates_strict_mode_rejects_low_or_unmeasured_posts() -> None:
    ranked = rank_viral_candidates(
        [
            {"tweet_id": "10", "text": "low", "impressions": 699_999},
            {"tweet_id": "20", "text": "unknown"},
        ],
        700_000,
        allow_below_threshold=False,
    )
    assert ranked == []


@pytest.mark.asyncio
async def test_create_engagement_draft_operator_approved() -> None:
    """Verifies that operator_approved sets DRAFT_APPROVED and passes verification."""
    settings = make_settings()
    mock_llm = MagicMock()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def add(self, obj):
            obj.id = 99

        async def flush(self):
            pass

        async def commit(self):
            pass

    mock_factory = MagicMock(return_value=FakeSession())
    service = EngagementService(settings, mock_llm, mock_factory)

    draft = await service.create_engagement_draft(
        account_id=1,
        target_post_url="https://x.com/IndianTechGuide/status/1885544332211",
        content="Automated banger quote",
        action="quote",
        style="quote_tweet",
        operator_approved=True,
    )

    assert draft.status == "approved"
    assert draft.verification_report.get("passed") is True
    assert draft.policy_report.get("operator_approved") is True


@pytest.mark.asyncio
async def test_publish_engagement_auto_approves_when_outbound_enabled() -> None:
    """Verifies publish_engagement auto-approves and dispatches when outbound engagement is enabled."""
    settings = make_settings(outbound_engagement_enabled=True, publishing_enabled=True, dry_run=False)
    mock_llm = MagicMock()
    mock_publisher = MagicMock()
    mock_publisher.publish = AsyncMock()

    from app.memory.models import Draft

    fake_draft = Draft(
        account_id=1,
        kind="reply",
        status="needs_review",
        idempotency_key="k1",
        body="Nice reply",
        policy_report={},
        verification_report={},
    )
    fake_draft.id = 123

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, draft_id):
            return fake_draft

        async def commit(self):
            pass

    mock_factory = MagicMock(return_value=FakeSession())
    service = EngagementService(settings, mock_llm, mock_factory, publisher=mock_publisher)

    await service.publish_engagement(fake_draft)
    assert fake_draft.status == "approved"
    mock_publisher.publish.assert_called_once()
