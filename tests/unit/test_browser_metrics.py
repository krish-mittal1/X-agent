from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.analytics.metrics import METRIC_FIELDS, AnalyticsService
from app.config import Settings
from app.memory.models import Account, ActionLog, Post, PostMetric
from app.publishing.x_client import XPermissionError


def setup_service(tweets, *, client=None, **overrides):
    settings = Settings(
        _env_file=None, app_env="test", llm_provider="disabled",
        x_publishing_mode="brave_cdp", analytics_enabled=True,
    ).model_copy(update=overrides)
    session = AsyncMock()
    session.add = MagicMock()
    session.get.return_value = Account(id=7, handle="@owner")
    session.scalars.return_value = MagicMock(all=lambda: [
        Post(id=i, account_id=7, x_post_id=str(i), published_at=datetime.now(UTC))
        for i in range(1, 5)
    ])
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    scanner = MagicMock()
    scanner.scan_profile_tweets = AsyncMock(return_value=tweets)
    service = AnalyticsService(settings, factory, client, browser_scanner=scanner)
    return service, session, scanner


def tweet(x_id="1", **counts):
    return {"tweet_id": x_id, "url": f"https://x.com/owner/status/{x_id}",
            "author": "owner", "text": "post", **counts}


async def test_brave_none_client_automatically_scans_and_preserves_four_views(monkeypatch):
    service, session, scanner = setup_service([
        tweet(str(i), impressions=views) for i, views in enumerate([3, 8, 19, 22], 1)
    ])
    service.browser_scanner = None
    constructor = MagicMock(return_value=scanner)
    monkeypatch.setattr("app.publishing.brave_cdp.BraveCDPPublisher", constructor)
    result = await service.collect(7)
    assert result["partial"] == 4 and result["stored"] == 0
    scanner.scan_profile_tweets.assert_awaited_once_with("owner", limit=50)
    entries = [call.args[0] for call in session.add.call_args_list]
    assert all(isinstance(entry, ActionLog) for entry in entries)
    assert [entry.details["observed_counts"] for entry in entries] == [
        {"impressions": value} for value in [3, 8, 19, 22]
    ]
    assert all("bookmarks" in entry.details["missing_fields"] for entry in entries)
    assert all(entry.account_id == 7 for entry in entries)
    session.commit.assert_awaited_once()


async def test_measured_zero_is_retained_but_unknown_and_invalid_counts_are_missing():
    service, session, _ = setup_service([
        tweet(impressions=0, likes=None, replies=-1, reposts=True, quotes="3", bookmarks=2**31)
    ])
    result = await service.collect(7)
    assert result["partial"] == 1
    entry = session.add.call_args.args[0]
    assert entry.details["observed_counts"] == {"impressions": 0}
    assert entry.details["missing_fields"] == list(METRIC_FIELDS[1:])


async def test_only_matching_owned_posts_are_recorded_once():
    wrong_author = tweet("2", impressions=9)
    wrong_author["url"] = "https://x.com/other/status/2"
    wrong_id = tweet("3", impressions=9)
    wrong_id["url"] = "https://x.com/owner/status/4"
    service, session, _ = setup_service([
        tweet(impressions=3), tweet(impressions=3), wrong_author, wrong_id,
        tweet("999", impressions=10), tweet("4"),
    ])
    result = await service.collect(7)
    assert result["partial"] == 1 and result["unobserved"] == 3
    assert session.add.call_count == 1


@pytest.mark.parametrize("tweets", [[], [tweet()]])
async def test_empty_or_unmeasured_scan_does_not_create_zero_snapshots(tweets):
    service, session, _ = setup_service(tweets)
    result = await service.collect(7)
    assert result["status"] == "skipped" and result["stored"] == 0
    session.add.assert_not_called()


async def test_scan_failure_is_reported_without_writes():
    service, session, scanner = setup_service([])
    scanner.scan_profile_tweets.side_effect = RuntimeError("browser unavailable")
    assert (await service.collect(7))["status"] == "failed"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("overrides", [{"analytics_enabled": False}])
async def test_disabled_analytics_does_not_scan(overrides):
    service, session, scanner = setup_service([], **overrides)
    assert (await service.collect(7))["status"] == "skipped"
    scanner.scan_profile_tweets.assert_not_awaited()
    session.add.assert_not_called()


async def test_disabled_reads_never_call_configured_api():
    client = MagicMock(configured=True, get_posts_metrics=AsyncMock())
    service, _, scanner = setup_service([tweet(impressions=3)], client=client)
    await service.collect(7)
    client.get_posts_metrics.assert_not_awaited()
    scanner.scan_profile_tweets.assert_awaited_once()


async def test_permission_error_falls_back_to_browser():
    client = MagicMock(configured=True, get_posts_metrics=AsyncMock(
        side_effect=XPermissionError("no read access")))
    service, _, scanner = setup_service(
        [tweet(impressions=3)], client=client,
        x_publishing_mode="api", x_api_enabled=True, x_reads_enabled=True,
    )
    assert (await service.collect(7))["partial"] == 1
    client.get_posts_metrics.assert_awaited_once()
    scanner.scan_profile_tweets.assert_awaited_once()


async def test_complete_api_snapshot_is_stored_without_browser():
    counts = dict.fromkeys(METRIC_FIELDS, 0)
    client = MagicMock(configured=True, get_posts_metrics=AsyncMock(return_value={"1": counts}))
    service, session, scanner = setup_service(
        [], client=client, x_publishing_mode="api", x_api_enabled=True, x_reads_enabled=True,
    )
    result = await service.collect(7)
    assert result["stored"] == 1
    metric = session.add.call_args.args[0]
    assert isinstance(metric, PostMetric)
    assert all(getattr(metric, field) == 0 for field in METRIC_FIELDS)
    scanner.scan_profile_tweets.assert_not_awaited()
