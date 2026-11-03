"""Safety regression tests for destructive integration-test database setup."""

from __future__ import annotations

import pytest

from tests.conftest import _validated_test_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://test:test@localhost:5432/x_agent_test",
        "postgresql+asyncpg://test:test@127.0.0.1:5432/techx_test",
    ],
)
def test_allows_only_explicit_loopback_test_databases(url: str) -> None:
    assert _validated_test_database_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://test:test@localhost:5432/techx",
        "postgresql+asyncpg://test:test@localhost:5432/x_agent",
        "postgresql+asyncpg://test:test@db.internal:5432/x_agent_test",
        "postgresql://test:test@localhost:5432/x_agent_test",
    ],
)
def test_rejects_production_remote_or_wrong_driver_database_urls(url: str) -> None:
    with pytest.raises(RuntimeError):
        _validated_test_database_url(url)
