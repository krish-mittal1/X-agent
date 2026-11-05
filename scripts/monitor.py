"""Operational monitor: detect silent failure and make noise about it.

The container staying green proves nothing. Every real outage mode in this
project looks healthy from the outside:

- the browser cookie session expires, so every publish fails quietly
- the scheduler stops ticking while uvicorn keeps answering /health
- the database fills with failures, but nobody reads action_logs

This script checks the things that actually matter and exits non-zero when one
of them breaks, so cron, a systemd timer, or the compose sidecar can alert.

Usage:
  python -m scripts.monitor
  python -m scripts.monitor --json
  python -m scripts.monitor --max-post-age-hours 12 --max-publish-failures 5

Environment:
  MONITOR_BASE_URL     API origin to probe (default http://127.0.0.1:API_PORT)
  ALERT_WEBHOOK_URL    optional; receives {"text": ..., "content": ...} JSON,
                       which Slack, Discord and ntfy-style endpoints accept

Exit codes: 0 = healthy, 1 = at least one check failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import Settings
from app.memory.database import dispose_engine, get_engine, init_engine
from scripts._common import prepare

AUTH_HINTS = (
    "auth",
    "cookie",
    "cookiecheck",
    "login",
    "logged out",
    "401",
    "403",
    "unauthorized",
    "session expired",
    "credentials",
)


@dataclass
class Finding:
    name: str
    ok: bool
    detail: str


@dataclass
class MonitorReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.findings.append(Finding(name=name, ok=ok, detail=detail))

    @property
    def failures(self) -> list[Finding]:
        return [finding for finding in self.findings if not finding.ok]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "healthy": self.healthy,
            "checks": [
                {"check": finding.name, "ok": finding.ok, "detail": finding.detail}
                for finding in self.findings
            ],
        }

    def as_text(self, *, include_passing: bool = True) -> str:
        lines = [f"x-agent monitor @ {datetime.now(UTC).isoformat(timespec='seconds')}"]
        for finding in self.findings:
            if finding.ok and not include_passing:
                continue
            status = "ok" if finding.ok else "FAIL"
            lines.append(f"  {status:<8} {finding.name:<15} {finding.detail}")
        if self.healthy:
            lines.append("  result: healthy")
        else:
            lines.append(f"  result: {len(self.failures)} check(s) failed")
        return "\n".join(lines)


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, dict[str, object] | None]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
            return response.status, _parse_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _parse_json(raw)


def _parse_json(raw: str) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def check_api(report: MonitorReport, base_url: str) -> None:
    """Liveness and readiness, the same probes the container healthcheck uses."""
    try:
        status, body = _http_get(f"{base_url}/health")
    except OSError as exc:
        report.add("api", False, f"unreachable at {base_url}: {type(exc).__name__}")
        return
    report.add(
        "api",
        status == 200,
        f"/health returned {status}" if status != 200 else f"{base_url} responding",
    )

    try:
        ready_status, ready_body = _http_get(f"{base_url}/ready")
    except OSError as exc:
        report.add("ready", False, f"/ready unreachable: {type(exc).__name__}")
        return
    if ready_status == 200:
        report.add("ready", True, "database, pgvector and migrations ok")
        return
    failed = []
    checks = (ready_body or {}).get("checks")
    if isinstance(checks, dict):
        failed = [
            name
            for name, value in checks.items()
            if isinstance(value, dict) and not value.get("ok")
        ]
    report.add(
        "ready",
        False,
        f"/ready returned {ready_status}" + (f"; failing: {', '.join(failed)}" if failed else ""),
    )


async def _scalar(conn, sql: str, **params):  # type: ignore[no-untyped-def]
    return await conn.scalar(text(sql), params)


async def check_database(
    report: MonitorReport,
    settings: Settings,
    *,
    max_post_age_hours: float,
    max_publish_failures: int,
    failure_window_hours: int,
) -> None:
    outbound = settings.publishing_enabled and not settings.dry_run and not settings.emergency_pause
    mode = settings.x_publishing_mode
    if not outbound:
        report.add("credentials", True, "not live: publishing disabled, dry run or paused")
    elif mode == "brave_cdp":
        missing = [
            name
            for name, value in (("X_AUTH_TOKEN", settings.x_auth_token), ("X_CT0", settings.x_ct0))
            if not value
        ]
        report.add(
            "credentials",
            not missing,
            f"missing {', '.join(missing)} for brave_cdp publishing"
            if missing
            else "browser session cookies present",
        )
    elif mode == "official_api":
        report.add(
            "credentials",
            settings.x_write_credentials_present,
            "OAuth 1.0a credentials present"
            if settings.x_write_credentials_present
            else "official_api publishing without X_CONSUMER_*/X_ACCESS_* credentials",
        )
    else:
        report.add("credentials", True, f"mode={mode}")

    try:
        async with get_engine().connect() as conn:
            last_post = await _scalar(conn, "SELECT max(published_at) FROM posts")
            recent_failures = await _scalar(
                conn,
                "SELECT count(*) FROM action_logs "
                "WHERE action LIKE 'publish%' AND outcome IN ('failed','unknown') "
                "AND created_at >= now() - (:hours * interval '1 hour')",
                hours=failure_window_hours,
            )
            latest_failure = await _scalar(
                conn,
                "SELECT details::text FROM action_logs "
                "WHERE action LIKE 'publish%' AND outcome = 'failed' "
                "ORDER BY created_at DESC LIMIT 1",
            )
            last_job = await _scalar(conn, "SELECT max(started_at) FROM job_runs")
            last_publish_job = await _scalar(
                conn, "SELECT max(started_at) FROM job_runs WHERE job_name = 'publishing'"
            )
            stuck_jobs = await _scalar(
                conn,
                "SELECT count(*) FROM job_runs "
                "WHERE status = 'running' AND started_at < now() - interval '30 minutes'",
            )
    except Exception as exc:  # the monitor must never crash; report it instead
        report.add("database", False, f"query failed: {type(exc).__name__}: {str(exc)[:160]}")
        return

    report.add("database", True, "reachable")

    now = datetime.now(UTC)
    if last_post is None:
        if outbound:
            report.add("post-freshness", False, "no post has ever been recorded while live")
        else:
            report.add("post-freshness", True, "no post recorded yet (dry run, expected)")
    else:
        age_hours = (now - last_post).total_seconds() / 3600
        report.add(
            "post-freshness",
            age_hours <= max_post_age_hours,
            f"last post {age_hours:.1f}h ago (limit {max_post_age_hours:g}h)",
        )

    report.add(
        "publish-failures",
        int(recent_failures or 0) < max_publish_failures,
        f"{int(recent_failures or 0)} failed/unknown publish(es) in {failure_window_hours}h "
        f"(limit {max_publish_failures})",
    )

    if latest_failure and any(hint in str(latest_failure).lower() for hint in AUTH_HINTS):
        report.add(
            "cookie-session",
            False,
            "most recent publish failure looks like an auth/session problem: re-copy "
            "X_AUTH_TOKEN and X_CT0, then restart the app",
        )
    else:
        report.add("cookie-session", True, "no auth-shaped publish failure recorded")

    grace_minutes = max(15, settings.ingest_interval_minutes)
    if last_job is None:
        report.add(
            "scheduler",
            False,
            "no job has ever run: is SCHEDULER_ENABLED=true and was bootstrap run?",
        )
    else:
        idle_minutes = (now - last_job).total_seconds() / 60
        report.add(
            "scheduler",
            idle_minutes <= grace_minutes * 2,
            f"last job {idle_minutes:.0f} min ago (max {grace_minutes * 2} min)",
        )

    if settings.publishing_enabled and not settings.dry_run:
        publish_grace = max(30, settings.publish_interval_minutes * 2 + 15)
        if last_publish_job is None:
            report.add(
                "publish-loop",
                False,
                "the publishing job has never run",
            )
        else:
            publish_idle = (now - last_publish_job).total_seconds() / 60
            report.add(
                "publish-loop",
                publish_idle <= publish_grace,
                f"last publishing slot {publish_idle:.0f} min ago (max {publish_grace} min)",
            )

    report.add(
        "stuck-jobs",
        int(stuck_jobs or 0) == 0,
        f"{int(stuck_jobs or 0)} job(s) marked running for over 30 min",
    )


def send_alert(webhook_url: str, message: str) -> bool:
    payload = json.dumps({"text": message, "content": message}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


async def run(
    settings: Settings,
    *,
    base_url: str,
    max_post_age_hours: float,
    max_publish_failures: int,
    failure_window_hours: int,
) -> MonitorReport:
    report = MonitorReport()
    await asyncio.to_thread(check_api, report, base_url)
    await check_database(
        report,
        settings,
        max_post_age_hours=max_post_age_hours,
        max_publish_failures=max_publish_failures,
        failure_window_hours=failure_window_hours,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the agent is actually working.")
    parser.add_argument(
        "--max-post-age-hours",
        type=float,
        default=float(os.environ.get("MAX_POST_AGE_HOURS", "8")),
        help="alert when the newest post is older than this (default: 8)",
    )
    parser.add_argument(
        "--max-publish-failures",
        type=int,
        default=int(os.environ.get("MAX_PUBLISH_FAILURES", "3")),
        help="alert at this many failed/unknown publishes in the window (default: 3)",
    )
    parser.add_argument(
        "--failure-window-hours",
        type=int,
        default=int(os.environ.get("FAILURE_WINDOW_HOURS", "24")),
        help="lookback window for publish failures (default: 24)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MONITOR_BASE_URL"),
        help="API origin to probe (default: http://127.0.0.1:API_PORT)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only report failures (useful for the compose sidecar log)",
    )
    args = parser.parse_args(argv)

    settings = prepare()
    host = settings.api_host if settings.api_host not in ("0.0.0.0", "::") else "127.0.0.1"
    base_url = (args.base_url or f"http://{host}:{settings.api_port}").rstrip("/")

    async def _main(report_holder: list[MonitorReport]) -> None:
        init_engine(settings)
        try:
            report_holder.append(
                await run(
                    settings,
                    base_url=base_url,
                    max_post_age_hours=args.max_post_age_hours,
                    max_publish_failures=args.max_publish_failures,
                    failure_window_hours=args.failure_window_hours,
                )
            )
        finally:
            await dispose_engine()

    holder: list[MonitorReport] = []
    asyncio.run(_main(holder))
    report = holder[0]

    if args.as_json:
        print(json.dumps(report.as_dict(), indent=2))
    elif not (args.quiet and report.healthy):
        print(report.as_text(include_passing=not args.quiet))

    if report.healthy:
        return 0

    webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    summary = "\n".join(f"- {f.name}: {f.detail}" for f in report.failures)
    message = f"x-agent needs attention\n{summary}"
    if webhook_url:
        delivered = asyncio.run(asyncio.to_thread(send_alert, webhook_url, message))
        if not delivered:
            print(
                "warning: ALERT_WEBHOOK_URL is set but the POST failed; "
                "this alert was only printed",
                flush=True,
            )
    else:
        print("note: ALERT_WEBHOOK_URL is not set, so this alert was only printed", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
