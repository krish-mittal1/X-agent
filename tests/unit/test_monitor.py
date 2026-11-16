from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from scripts import monitor


class FakeConnection:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    async def scalar(self, statement, params):  # type: ignore[no-untyped-def]
        sql = str(statement)
        if "max(published_at)" in sql:
            return datetime.now(UTC) - timedelta(days=30)
        if "count(*) FROM action_logs" in sql:
            return 0
        if "details::text" in sql:
            return None
        if "max(started_at)" in sql:
            return datetime.now(UTC) - timedelta(days=30)
        if "count(*) FROM job_runs" in sql:
            return 0
        return None


@pytest.mark.asyncio
async def test_disabled_outbound_and_scheduler_do_not_raise_freshness_alarms(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = MagicMock()
    engine.connect.return_value = FakeConnection()
    monkeypatch.setattr(monitor, "get_engine", lambda: engine)
    settings = Settings(
        _env_file=None,
        publishing_enabled=False,
        dry_run=True,
        scheduler_enabled=False,
    )
    report = monitor.MonitorReport()

    await monitor.check_database(
        report,
        settings,
        max_post_age_hours=6,
        max_publish_failures=3,
        failure_window_hours=24,
    )

    checks = {check.name: check for check in report.findings}
    assert checks["post-freshness"].ok is True
    assert "disabled" in checks["post-freshness"].detail
    assert checks["scheduler"].ok is True
    assert checks["scheduler"].detail == "disabled by configuration"
