"""Engagement metrics retrieval and evaluation.

Use API reads when enabled, otherwise collect visible counts through Brave.
Partial observations are audited, not coerced into complete PostMetric rows:
the current schema and report consumers cannot represent unknown counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import Account, Post, PostMetric
from app.memory.repositories import AuditRepository
from app.publishing.x_client import XError, XPermissionError

logger = get_logger(__name__)

METRIC_FIELDS = ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks")


class ProfileScanner(Protocol):
    async def scan_profile_tweets(self, handle: str, limit: int = 3) -> list[dict[str, Any]]: ...


def _observed_counts(values: dict[str, Any]) -> dict[str, int]:
    """Accept explicit nonnegative integer counts, including measured zero."""
    return {
        field: values[field]
        for field in METRIC_FIELDS
        if type(values.get(field)) is int and 0 <= values[field] <= 2_147_483_647
    }


def _profile_post_id(tweet: dict[str, Any], handle: str) -> str | None:
    # Scanner's author is the requested handle, even for reposted articles.
    # Verify the URL instead, and require its ID to agree with tweet_id.
    url = urlparse(str(tweet.get("url", "")))
    parts = url.path.strip("/").split("/")
    if (
        url.scheme != "https"
        or url.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or len(parts) != 3
        or parts[0].casefold() != handle.casefold()
        or parts[1] != "status"
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or str(tweet.get("tweet_id", "")) != parts[2]
    ):
        return None
    return parts[2]


class AnalyticsService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        x_client,  # type: ignore[no-untyped-def]
        *,
        clock=None,  # type: ignore[no-untyped-def]
        browser_scanner: ProfileScanner | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.x_client = x_client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.browser_scanner = browser_scanner

    async def collect(self, account_id: int, *, limit: int = 50) -> dict[str, object]:
        if not self.settings.analytics_enabled:
            return {"status": "skipped", "reason": "analytics disabled"}
        if limit < 1:
            return {"status": "skipped", "reason": "limit must be positive"}
        api_available = (
            self.settings.x_reads_enabled
            and not self.settings.is_zero_cost_mode
            and self.x_client is not None
            and getattr(self.x_client, "configured", False)
        )
        async with self.session_factory() as session:
            posts = list(
                (
                    await session.scalars(
                        select(Post)
                        .where(
                            Post.account_id == account_id,
                            Post.x_post_id.is_not(None),
                            Post.published_at.is_not(None),
                        )
                        .order_by(Post.published_at.desc())
                        .limit(limit)
                    )
                ).all()
            )
            if not posts:
                return {"status": "ok", "stored": 0, "reason": "no published posts"}
            ids = [str(post.x_post_id) for post in posts if post.x_post_id]
            if not api_available:
                return await self._collect_browser(session, account_id, posts, limit=limit)
            try:
                metrics = await self.x_client.get_posts_metrics(ids)
            except XPermissionError as exc:
                logger.warning("metrics unavailable with current API access",
                               extra={"extra_fields": {"error": str(exc)[:200]}})
                return await self._collect_browser(
                    session, account_id, posts, limit=limit,
                    unavailable_reason="insufficient X API read access",
                )
            except XError as exc:
                logger.warning("metrics fetch failed",
                               extra={"extra_fields": {"error": str(exc)[:200]}})
                return {"status": "failed", "reason": str(exc)[:200]}
            by_x_id = {str(post.x_post_id): post for post in posts}
            stored = 0
            partial = 0
            for x_id, values in metrics.items():
                post = by_x_id.get(x_id)
                if post is None:
                    continue
                counts = _observed_counts(values)
                if len(counts) != len(METRIC_FIELDS):
                    if counts:
                        await self._audit_partial(session, account_id, post, counts, "x_api")
                        partial += 1
                    continue
                session.add(
                    PostMetric(
                        post_id=post.id,
                        **counts,
                        fetched_at=self.clock(),
                        raw=values,
                    )
                )
                stored += 1
            await session.commit()
        return {"status": "partial" if partial else "ok", "stored": stored, "partial": partial}

    async def _audit_partial(
        self, session: AsyncSession, account_id: int, post: Post,
        counts: dict[str, int], source: str,
    ) -> None:
        await AuditRepository.log(
            session, account_id=account_id, action="analytics.metrics_observed", outcome="partial",
            details={
                "post_id": post.id, "x_post_id": post.x_post_id, "source": source,
                "fetched_at": self.clock().isoformat(), "observed_counts": counts,
                "missing_fields": [field for field in METRIC_FIELDS if field not in counts],
                "count_semantics": "visible_public_counts" if source == "brave_cdp" else "api_counts",
            },
        )

    async def _collect_browser(
        self, session: AsyncSession, account_id: int, posts: list[Post], *, limit: int,
        unavailable_reason: str = "X API reads unavailable and no browser scanner configured",
    ) -> dict[str, object]:
        scanner = self.browser_scanner
        if scanner is None:
            if self.settings.x_publishing_mode != "brave_cdp":
                return {"status": "skipped", "reason": unavailable_reason}
            from app.publishing.brave_cdp import BraveCDPPublisher

            scanner = BraveCDPPublisher(self.settings, self.session_factory)
        account = await session.get(Account, account_id)
        if account is None or not account.handle.strip().lstrip("@"):
            return {"status": "skipped", "reason": "account handle unavailable"}
        handle = account.handle.strip().lstrip("@")
        try:
            tweets = await scanner.scan_profile_tweets(handle, limit=limit)
        except Exception:
            logger.warning("browser metrics scan failed", exc_info=True)
            return {"status": "failed", "stored": 0, "reason": "browser metrics scan failed"}
        by_x_id = {str(post.x_post_id): post for post in posts}
        seen: set[str] = set()
        stored = partial = 0
        for tweet in tweets:
            x_id = _profile_post_id(tweet, handle)
            if x_id not in by_x_id or x_id in seen:
                continue
            counts = _observed_counts(tweet)
            if not counts:
                continue
            seen.add(x_id)
            post = by_x_id[x_id]
            if len(counts) == len(METRIC_FIELDS):
                session.add(PostMetric(
                    post_id=post.id, **counts, fetched_at=self.clock(),
                    raw={"source": "brave_cdp", "observed_counts": counts,
                         "count_semantics": "visible_public_counts"},
                ))
                stored += 1
            else:
                await self._audit_partial(session, account_id, post, counts, "brave_cdp")
                partial += 1
        await session.commit()
        return {
            "status": "partial" if partial else ("ok" if stored else "skipped"),
            "source": "brave_cdp", "stored": stored, "partial": partial,
            "unobserved": len(by_x_id) - len(seen),
            "reason": (
                "partial counts saved in action_logs; unknown counts are not zero"
                if partial else ("visible counts collected" if stored else "no matching visible counts")
            ),
        }
