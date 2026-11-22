from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.jobs.runner import JobContext
from app.jobs.tasks import outbound_engagement
from app.memory.models import Account
from app.schemas.publishing import PublishResult


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
        "outbound_engagement_enabled": True,
        "max_replies_per_day": 5,
        "api_expenditure_usd": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_outbound_engagement_skips_when_disabled() -> None:
    settings = make_settings(outbound_engagement_enabled=False)
    context = MagicMock(spec=JobContext)
    context.settings = settings
    res = await outbound_engagement(context)
    assert res["status"] == "skipped"
    assert "disabled" in res["reason"]


@pytest.mark.asyncio
async def test_outbound_engagement_skips_when_dry_run() -> None:
    settings = make_settings(dry_run=True)
    context = MagicMock(spec=JobContext)
    context.settings = settings
    res = await outbound_engagement(context)
    assert res["status"] == "skipped"
    assert "dry run" in res["reason"]


@pytest.mark.asyncio
async def test_outbound_engagement_skips_when_daily_limit_reached() -> None:
    settings = make_settings(max_replies_per_day=2)
    context = MagicMock(spec=JobContext)
    context.settings = settings

    fake_account = Account(handle="ssh_bugs", is_active=True)
    fake_account.id = 1

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, stmt):
            sql = str(stmt).lower()
            if "accounts" in sql:
                return fake_account
            if "count" in sql:
                return 2
            return None

        async def scalars(self, stmt):
            m = MagicMock()
            m.first.return_value = fake_account
            m.all.return_value = []
            return m

    context.session_factory = MagicMock(return_value=FakeSession())
    res = await outbound_engagement(context)
    assert res["status"] == "skipped"
    assert "daily reply limit reached" in res["reason"]


@pytest.mark.asyncio
async def test_outbound_engagement_dispatches_live_and_likes() -> None:
    settings = make_settings()
    context = MagicMock(spec=JobContext)
    context.settings = settings
    context.llm = MagicMock()

    fake_account = Account(handle="ssh_bugs", is_active=True)
    fake_account.id = 1

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, stmt):
            sql = str(stmt).lower()
            if "accounts" in sql:
                return fake_account
            if "count" in sql:
                return 0
            return None

        async def scalars(self, stmt):
            sql = str(stmt)
            m = MagicMock()
            if "accounts" in sql.lower():
                m.first.return_value = fake_account
            else:
                m.all.return_value = []
            return m

        def add(self, obj):
            obj.id = 77

        async def flush(self):
            pass

        async def commit(self):
            pass

    context.session_factory = MagicMock(return_value=FakeSession())

    mock_publisher = MagicMock()
    mock_publisher.is_cdp_available = AsyncMock(return_value=True)

    with (
        patch("app.publishing.brave_cdp.BraveCDPPublisher", return_value=mock_publisher),
        patch("app.publishing.brave_cdp.resolve_brave_executable", return_value="/usr/bin/chromium"),
        patch("app.growth.engagement.EngagementService.scan_handles_for_candidates", new_callable=AsyncMock) as mock_scan,
        patch("app.growth.engagement.EngagementService.generate_bangers", new_callable=AsyncMock) as mock_gen,
        patch("app.growth.engagement.EngagementService.publish_engagement", new_callable=AsyncMock) as mock_publish,
    ):
        mock_scan.return_value = [
            {
                "url": "https://x.com/IndianTechGuide/status/123456",
                "tweet_id": str((int(datetime.now(UTC).timestamp() * 1000) - 1288834974657) << 22),
                "author": "IndianTechGuide",
                "text": "Breaking tech update from Hyderabad",
                "impressions": 50000,
            }
        ]
        mock_gen.return_value = {
            "sarcastic": "Peak sarcasm",
            "quote_tweet": "Lyssa style quote banger",
            "bakchodi": "Desi humor take",
            "rage_bait": "Spicy opinion",
            "value_add": "The actual cost is high",
        }
        mock_publish.return_value = PublishResult(
            status="published",
            draft_id=77,
            post_ids=["status_998877"],
            reasons=[],
        )

        res = await outbound_engagement(context)

        assert res["status"] == "published"
        assert res["target_url"] == "https://x.com/IndianTechGuide/status/123456"
        assert res["post_ids"] == ["status_998877"]
        mock_publish.assert_called_once()


@pytest.mark.asyncio
async def test_outbound_engagement_handles_errors_gracefully() -> None:
    settings = make_settings()
    context = MagicMock(spec=JobContext)
    context.settings = settings
    context.llm = MagicMock()

    fake_account = Account(handle="ssh_bugs", is_active=True)
    fake_account.id = 1

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, stmt):
            sql = str(stmt).lower()
            if "accounts" in sql:
                return fake_account
            if "count" in sql:
                return 0
            return None

        async def scalars(self, stmt):
            m = MagicMock()
            m.first.return_value = fake_account
            return m

    context.session_factory = MagicMock(return_value=FakeSession())

    mock_publisher = MagicMock()
    mock_publisher.is_cdp_available = AsyncMock(return_value=True)

    with (
        patch("app.publishing.brave_cdp.BraveCDPPublisher", return_value=mock_publisher),
        patch("app.publishing.brave_cdp.resolve_brave_executable", return_value="/usr/bin/chromium"),
        patch("app.growth.engagement.EngagementService.scan_handles_for_candidates", new_callable=AsyncMock) as mock_scan,
    ):
        mock_scan.side_effect = RuntimeError("Playwright connection refused")

        res = await outbound_engagement(context)
        assert res["status"] == "failed"
        assert "Playwright connection refused" in res["reason"]
