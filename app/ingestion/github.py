"""GitHub release discovery through the official REST API.

Only allowlisted repositories (plus optional search queries) are read. Draft and
(default) pre-releases are skipped, results are cached per process, and the
adapter honours rate-limit headers instead of hammering the API.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.ingestion.base import AdapterResult
from app.ingestion.extract import clean_excerpt, strip_html_fragment
from app.ingestion.source_registry import classify_source
from app.logging_config import get_logger
from app.net import RetryPolicy, retry_async
from app.schemas.research import IngestedItem

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
MAX_CONTENT_CHARS = 30_000
_CACHE_TTL_SECONDS = 3600


class GitHubError(RuntimeError):
    pass


class GitHubRateLimited(GitHubError):
    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubAdapter:
    name = "github"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        repositories: tuple[str, ...] | None = None,
        search_queries: tuple[str, ...] | None = None,
        sleep=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.client = client
        self.repositories = (
            repositories if repositories is not None else settings.repositories
        )
        self.search_queries = (
            search_queries if search_queries is not None else settings.github_queries
        )
        self._repo_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Per-adapter duplicate set; the database also enforces URL uniqueness.
        self._seen_external_ids: set[str] = set()
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.settings.http_user_agent,
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token.get_secret_value()}"
        return headers

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async def _once() -> httpx.Response:
            return await self.client.get(
                f"{GITHUB_API}{path}",
                params=params,
                headers=self._headers(),
                timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            )

        async def _fetch() -> Any:
            response = await _once()
            if response.status_code in {403, 429}:
                remaining = response.headers.get("x-ratelimit-remaining")
                reset_header = response.headers.get("x-ratelimit-reset")
                reset_at = (
                    datetime.fromtimestamp(int(reset_header), tz=UTC)
                    if reset_header and reset_header.isdigit()
                    else None
                )
                if remaining == "0" or response.status_code == 429:
                    raise GitHubRateLimited(
                        "GitHub API rate limit exhausted", reset_at=reset_at
                    )
                raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"GitHub server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if response.status_code >= 400:
                raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")
            return response.json()

        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return await retry_async(
            _fetch,
            RetryPolicy(attempts=3, base_delay=1.0, max_delay=8.0),
            retry_on=(httpx.TransportError, httpx.HTTPStatusError),
            **kwargs,
        )

    async def _repo_metadata(self, repo: str) -> dict[str, Any]:
        cached = self._repo_cache.get(repo)
        now = time.monotonic()
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        data = await self._request(f"/repos/{repo}")
        self._repo_cache[repo] = (now, data)
        return data

    @staticmethod
    def _published_at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _releases_for_repo(self, repo: str) -> list[IngestedItem]:
        try:
            releases = await self._request(
                f"/repos/{repo}/releases", params={"per_page": 10}
            )
        except GitHubRateLimited:
            raise
        except GitHubError as exc:
            logger.warning("release fetch failed", extra={"extra_fields": {"repo": repo, "error": str(exc)[:200]}})
            return []
        if not isinstance(releases, list):
            return []
        metadata: dict[str, Any] = {}
        try:
            metadata = await self._repo_metadata(repo)
        except GitHubError as exc:
            logger.warning("repo metadata failed", extra={"extra_fields": {"repo": repo, "error": str(exc)[:200]}})

        items: list[IngestedItem] = []
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            if release.get("prerelease") and not self.settings.github_include_prereleases:
                continue
            release_id = str(release.get("id", ""))
            url = str(release.get("html_url") or "")
            if not release_id or not url:
                continue
            external_id = f"github:release:{release_id}"
            if external_id in self._seen_external_ids:
                continue
            self._seen_external_ids.add(external_id)

            tag = str(release.get("tag_name") or release.get("name") or "")
            body_raw = str(release.get("body") or "")
            body = strip_html_fragment(body_raw) if "<" in body_raw else body_raw
            repo_short = repo.split("/")[-1]
            title = f"{repo_short} {tag}".strip()
            if release.get("name") and str(release["name"]) not in title:
                title = f"{title} — {release['name']}".strip(" —")
            content = body.strip()
            if not content:
                content = f"Release {tag} published for {repo}."
            profile = classify_source("github", url)
            items.append(
                IngestedItem(
                    source_type="github",
                    source_identifier=repo,
                    url=url,
                    title=title,
                    content=content[:MAX_CONTENT_CHARS],
                    excerpt=clean_excerpt(content),
                    author=str(metadata.get("owner", {}).get("login") or repo.split("/")[0]),
                    published_at=self._published_at(
                        release.get("published_at") or release.get("created_at")
                    ),
                    reliability_score=profile.reliability,
                    external_id=external_id,
                    fetch_status="ok",
                    metadata={
                        "repo": repo,
                        "tag": tag,
                        "prerelease": bool(release.get("prerelease")),
                        "release_id": release_id,
                        "stars": metadata.get("stargazers_count"),
                        "description": metadata.get("description"),
                        "language": metadata.get("language"),
                        "topics": metadata.get("topics", []),
                        "is_primary_source": True,
                        "source_kind": "release_notes",
                    },
                )
            )
        return items

    async def _repos_from_search(self, query: str) -> list[str]:
        since = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
        q = query if "pushed:" in query else f"{query} pushed:>={since}"
        try:
            data = await self._request(
                "/search/repositories",
                params={"q": q, "sort": "updated", "order": "desc", "per_page": 3},
            )
        except GitHubRateLimited:
            raise
        except GitHubError as exc:
            logger.warning("repository search failed", extra={"extra_fields": {"query": query, "error": str(exc)[:200]}})
            return []
        repos: list[str] = []
        for item in data.get("items", []) if isinstance(data, dict) else []:
            if item.get("archived") or item.get("fork"):
                continue
            # Only allowlist-style owner/name identifiers, never arbitrary URLs.
            name = str(item.get("full_name") or "")
            if name.count("/") == 1 and all(name.split("/")):
                repos.append(name)
        return repos

    async def fetch(self) -> AdapterResult:
        result = AdapterResult()
        repos = list(self.repositories)
        for query in self.search_queries:
            try:
                repos.extend(r for r in await self._repos_from_search(query) if r not in repos)
            except GitHubRateLimited as exc:
                result.errors.append(f"GitHub rate limited during search: {exc}")
        for repo in dict.fromkeys(repos):
            if repo.count("/") != 1:
                result.errors.append(f"invalid repository identifier: {repo!r}")
                continue
            try:
                result.items.extend(await self._releases_for_repo(repo))
            except GitHubRateLimited as exc:
                result.errors.append(
                    f"GitHub rate limit reached (resets at {exc.reset_at}); stopping"
                )
                return result
            except (GitHubError, httpx.HTTPError) as exc:
                result.errors.append(f"{repo}: {exc}")
        return result
