"""Unit tests for Brave CDP publishing provider."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.memory.models import Draft
from app.publishing.brave_cdp import BraveCDPPublisher
from app.publishing.provider import get_publishing_provider


def make_brave_cdp_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "llm_provider": "disabled",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "rss_feeds": "",
        "github_repositories": "",
        "x_publishing_mode": "brave_cdp",
        "x_api_enabled": False,
        "publishing_enabled": True,
        "dry_run": False,
        "enable_reply_scanner": False,
        "enable_manual_reply_workflow": True,
        "api_expenditure_usd": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_brave_cdp_settings_validation() -> None:
    settings = make_brave_cdp_settings()
    assert settings.is_zero_cost_mode is True
    assert settings.x_publishing_mode == "brave_cdp"
    assert settings.publishing_enabled is True
    assert settings.dry_run is False
    assert settings.x_write_credentials_present is False


def test_get_publishing_provider_selects_brave_cdp() -> None:
    settings = make_brave_cdp_settings()
    mock_factory = MagicMock()
    provider = get_publishing_provider(settings, mock_factory)
    assert isinstance(provider, BraveCDPPublisher)


@pytest.mark.asyncio
async def test_brave_cdp_is_available_false_when_port_down() -> None:
    settings = make_brave_cdp_settings()
    mock_factory = MagicMock()
    provider = BraveCDPPublisher(settings, mock_factory, cdp_url="http://127.0.0.1:59999")
    assert await provider.is_cdp_available() is False


@pytest.mark.asyncio
async def test_brave_cdp_validate_connection_reports_token_requirement() -> None:
    # When tokens are missing, report need for X_AUTH_TOKEN and X_CT0
    settings = make_brave_cdp_settings(x_auth_token=None, x_ct0=None)
    mock_factory = MagicMock()
    provider = BraveCDPPublisher(settings, mock_factory, cdp_url="http://127.0.0.1:59999")
    val = await provider.validate_connection()
    assert val["valid"] is False
    assert "X_AUTH_TOKEN and X_CT0" in val["detail"]


@pytest.mark.asyncio
async def test_brave_cdp_validate_connection_succeeds_with_tokens() -> None:
    settings = make_brave_cdp_settings(
        x_auth_token="test_token",
        x_ct0="test_ct0",
    )
    mock_factory = MagicMock()
    provider = BraveCDPPublisher(settings, mock_factory)
    val = await provider.validate_connection()
    assert val["valid"] is True
    assert "ban-proof publishing active" in val["detail"]


@pytest.mark.asyncio
async def test_brave_cdp_publish_simulated_when_dry_run() -> None:
    settings = make_brave_cdp_settings(dry_run=True)
    mock_factory = MagicMock()
    provider = BraveCDPPublisher(settings, mock_factory)
    draft = Draft(id=1, account_id=1, kind="original", status="ready", body="Test post")
    res = await provider.publish(draft, ["Test post"])
    assert res.status == "simulated"


@pytest.mark.asyncio
async def test_brave_cdp_download_temp_media(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = make_brave_cdp_settings()
    mock_factory = MagicMock()
    provider = BraveCDPPublisher(settings, mock_factory)

    fake_bytes = b"GIF89a" + b"\x00" * 2000  # > 1500 bytes

    class MockResp:
        status_code = 200
        content = fake_bytes

    class MockClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    path = await provider._download_temp_media("https://example.com/photo.jpg")
    assert path is not None
    assert path.exists()
    assert path.suffix == ".jpg"
    assert len(path.read_bytes()) > 1500
    path.unlink()
