from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.publishing_enabled is False
    assert settings.dry_run is True
    assert settings.ai_reply_publishing_enabled is False
    assert settings.llm_enabled is False  # no key/model configured


def test_secrets_are_redacted() -> None:
    settings = Settings(
        _env_file=None,
        gemini_api_key=SecretStr("super-secret-key"),
        admin_api_token=SecretStr("admin-secret"),
        x_bearer_token=SecretStr("bearer"),
    )
    dumped = settings.redacted_dump()
    assert dumped["gemini_api_key"] == "set"
    assert "super-secret-key" not in str(dumped)
    assert "admin-secret" not in str(dumped)


def test_rejects_non_http_feed() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rss_feeds="file:///etc/passwd")


def test_rejects_bad_repository_format() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, github_repositories="owner-only")


def test_csv_parsing_and_deduplication() -> None:
    settings = Settings(
        _env_file=None,
        rss_feeds="https://a.example/feed, https://b.example/rss\nhttps://a.example/feed",
        github_repositories="openai/openai-python, htmx/htmx",
    )
    assert settings.feed_urls == ("https://a.example/feed", "https://b.example/rss")
    assert settings.repositories == ("openai/openai-python", "htmx/htmx")


def test_scoring_weights_are_normalised() -> None:
    settings = Settings(
        _env_file=None,
        weight_freshness=2,
        weight_technical_relevance=1,
        weight_novelty=1,
        weight_source_reliability=0,
        weight_evidence_quality=0,
        weight_audience_usefulness=0,
    )
    weights = settings.scoring_weights
    assert pytest.approx(sum(weights.values())) == 1.0
    assert weights["freshness"] == pytest.approx(0.5)


def test_ai_reply_sending_requires_approval_flag() -> None:
    with pytest.raises(ValidationError, match="AI_REPLY_APPROVAL_OBTAINED"):
        Settings(_env_file=None, ai_reply_publishing_enabled=True)


def test_live_publishing_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        Settings(
            _env_file=None,
            x_publishing_mode="official_api",
            x_api_enabled=True,
            publishing_enabled=True,
            dry_run=False,
        )


def test_zero_cost_mode_rejects_live_x_publishing() -> None:
    with pytest.raises(ValidationError, match="zero-cost mode"):
        Settings(_env_file=None, publishing_enabled=True, dry_run=False)


def test_production_requires_admin_token() -> None:
    with pytest.raises(ValidationError, match="ADMIN_API_TOKEN"):
        Settings(_env_file=None, app_env="production")


def test_non_asyncpg_database_url_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://user:pass@host/db")
