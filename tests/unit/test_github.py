from __future__ import annotations

from datetime import UTC

import httpx
import pytest

from app.config import Settings
from app.ingestion.github import GitHubAdapter


def make_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {"_env_file": None, "github_repositories": "acme/widget"}
    values.update(overrides)
    return Settings(**values)


def releases_payload() -> list[dict]:
    return [
        {
            "id": 1,
            "tag_name": "v2.0.0",
            "name": "Turbo mode",
            "body": "Adds **turbo mode**. Throughput improves to 1,200 tok/s.",
            "html_url": "https://github.com/acme/widget/releases/tag/v2.0.0",
            "published_at": "2026-10-27T10:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "id": 2,
            "tag_name": "v2.1.0-rc1",
            "name": "RC",
            "body": "release candidate",
            "html_url": "https://github.com/acme/widget/releases/tag/v2.1.0-rc1",
            "published_at": "2026-10-28T10:00:00Z",
            "draft": False,
            "prerelease": True,
        },
        {
            "id": 3,
            "tag_name": "v3.0.0-draft",
            "name": "Draft",
            "body": "not yet",
            "html_url": "https://github.com/acme/widget/releases/tag/v3.0.0-draft",
            "draft": True,
            "prerelease": False,
        },
        {
            "id": 4,
            "tag_name": "v2.0.1",
            "name": "",
            "body": "",
            "html_url": "https://github.com/acme/widget/releases/tag/v2.0.1",
            "published_at": "2026-10-26T10:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]


async def test_github_adapter_filters_drafts_and_prereleases() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases_payload())
        if request.url.path == "/repos/acme/widget":
            return httpx.Response(200, json={"stargazers_count": 4200, "description": "A widget",
                                             "topics": ["ai"], "owner": {"login": "acme"}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GitHubAdapter(make_settings(), client)
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()

    assert [item.metadata["release_id"] for item in result.items] == ["1", "4"]
    first = result.items[0]
    assert "turbo mode" in first.content.lower()
    assert first.metadata["stars"] == 4200
    assert first.metadata["is_primary_source"] is True
    assert first.published_at is not None and first.published_at.tzinfo == UTC
    assert result.items[1].content  # empty body still produces a usable note


async def test_github_adapter_deduplicates_within_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases_payload())
        return httpx.Response(200, json={"stargazers_count": 1, "owner": {"login": "acme"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GitHubAdapter(make_settings(), client)
    try:
        first = await adapter.fetch()
        second = await adapter.fetch()
    finally:
        await client.aclose()
    assert len(first.items) == 2
    assert second.items == []


async def test_github_rate_limit_is_reported_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"},
            json={"message": "API rate limit exceeded"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GitHubAdapter(make_settings(), client)
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()
    assert result.items == []
    assert any("rate limit" in error.lower() for error in result.errors)


async def test_github_invalid_repository_is_rejected() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    adapter = GitHubAdapter(make_settings(), client, repositories=("not-a-repo",))
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()
    assert result.items == []
    assert any("invalid repository" in error for error in result.errors)


@pytest.mark.parametrize("query", ["llm agents", "vllm"])
async def test_github_search_uses_repo_search_and_caps_repos(query: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/repositories":
            assert "pushed:" in request.url.params["q"]
            return httpx.Response(
                200,
                json={"items": [{"full_name": "acme/widget", "archived": False, "fork": False},
                                 {"full_name": "bad", "archived": False, "fork": False}]},
            )
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases_payload()[:1])
        return httpx.Response(200, json={"owner": {"login": "acme"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GitHubAdapter(make_settings(), client, repositories=(), search_queries=(query,))
    try:
        result = await adapter.fetch()
    finally:
        await client.aclose()
    assert len(result.items) == 1
    assert result.items[0].metadata["repo"] == "acme/widget"
