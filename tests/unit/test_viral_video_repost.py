from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.growth.video_repost import generate_video_caption
from app.jobs.runner import JobContext
from app.jobs.tasks import viral_video_repost
from app.memory.models import Account
from app.providers.base import LLMResponse, LLMUsage
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
        "viral_video_repost_enabled": True,
        "max_original_posts_per_day": 3,
        "api_expenditure_usd": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_viral_video_repost_skips_when_publishing_disabled() -> None:
    settings = make_settings(publishing_enabled=False)
    context = MagicMock(spec=JobContext)
    context.settings = settings
    res = await viral_video_repost(context)
    assert res["status"] == "skipped"
    assert "disabled" in res["reason"]


@pytest.mark.asyncio
async def test_viral_video_repost_skips_when_feature_disabled() -> None:
    settings = make_settings(viral_video_repost_enabled=False)
    context = MagicMock(spec=JobContext)
    context.settings = settings
    res = await viral_video_repost(context)
    assert res["status"] == "skipped"
    assert "disabled" in res["reason"]


@pytest.mark.asyncio
async def test_viral_video_repost_skips_when_daily_limit_reached() -> None:
    settings = make_settings(max_original_posts_per_day=2)
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
            return 2  # already reached 2 posts

        async def scalars(self, stmt):
            m = MagicMock()
            m.first.return_value = fake_account
            m.all.return_value = []
            return m

    context.session_factory = MagicMock(return_value=FakeSession())
    res = await viral_video_repost(context)
    assert res["status"] == "skipped"
    assert "daily original post limit reached" in res["reason"]


@pytest.mark.asyncio
async def test_generate_video_caption_with_keyframes() -> None:
    llm = MagicMock()
    llm.enabled = True
    llm.generate = AsyncMock(
        return_value=LLMResponse(
            text="CAPTION: Peak Bangalore behavior caught in 4K",
            model="gemini-2.5-flash",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=15),
            latency_ms=250.0,
            estimated_cost_usd=0.0001,
        )
    )

    fake_frame = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    caption = await generate_video_caption(
        llm,
        tweet_text="Auto driver accepting UPI via smart watch",
        keyframes=[fake_frame],
        tweet_author="peakbengaluru",
    )

    assert caption == "Peak Bangalore behavior caught in 4K"
    llm.generate.assert_called_once()
    call_args = llm.generate.call_args
    messages = call_args[0][0]
    # Check that multimodal payload was constructed
    assert isinstance(messages[1].content, list)
    assert any(part.get("type") == "image_url" for part in messages[1].content)


@pytest.mark.asyncio
async def test_generate_video_caption_text_only_fallback() -> None:
    llm = MagicMock()
    llm.enabled = True
    llm.generate = AsyncMock(
        return_value=LLMResponse(
            text="Corporate employees on Monday morning be like",
            model="gemini-2.5-flash",
            usage=LLMUsage(prompt_tokens=50, completion_tokens=10),
            latency_ms=200.0,
            estimated_cost_usd=0.00005,
        )
    )

    caption = await generate_video_caption(
        llm,
        tweet_text="Metro crowd fighting for seat in Rajiv Chowk",
        keyframes=[],
        tweet_author="gharkekalesh",
    )

    assert caption == "Corporate employees on Monday morning be like"
    messages = llm.generate.call_args[0][0]
    assert isinstance(messages[1].content, str)


@pytest.mark.asyncio
async def test_viral_video_repost_dispatches_successfully() -> None:
    settings = make_settings()
    context = MagicMock(spec=JobContext)
    context.settings = settings
    context.llm = MagicMock()
    context.llm.enabled = True

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
            sql = str(stmt).lower()
            m = MagicMock()
            if "accounts" in sql:
                return fake_account
            return m

        def add(self, obj):
            obj.id = 88

        async def flush(self):
            pass

        async def commit(self):
            pass

    context.session_factory = MagicMock(return_value=FakeSession())

    mock_candidates = [
        {
            "url": "https://x.com/gharkekalesh/status/199887766",
            "tweet_id": "199887766",
            "author": "gharkekalesh",
            "text": "Wild fight inside Delhi Metro today",
            "impressions": 500_000,
            "has_video": True,
        }
    ]

    mock_publisher = MagicMock()
    mock_publisher.publish_with_video = AsyncMock(
        return_value=PublishResult(
            status="published",
            draft_id=88,
            post_ids=["status_55443322"],
            reasons=[],
        )
    )

    with (
        patch("app.growth.video_repost.scan_viral_handles_for_video_posts", new_callable=AsyncMock) as mock_scan,
        patch("app.growth.video_repost.download_tweet_video", new_callable=AsyncMock) as mock_download,
        patch("app.growth.video_repost._extract_keyframes_pillow", return_value=[b"fake_jpg"]),
        patch("app.growth.video_repost.generate_video_caption", new_callable=AsyncMock) as mock_gen_caption,
        patch("app.publishing.brave_cdp.BraveCDPPublisher", return_value=mock_publisher),
    ):
        mock_scan.return_value = mock_candidates
        mock_download.return_value = Path("/tmp/fake_video.mp4")
        mock_gen_caption.return_value = "Average day in Delhi Metro"

        res = await viral_video_repost(context)
        assert res["status"] == "published"
        assert res["caption"] == "Average day in Delhi Metro"
        assert res["post_ids"] == ["status_55443322"]
        mock_download.assert_called_once()
        mock_gen_caption.assert_called_once()
