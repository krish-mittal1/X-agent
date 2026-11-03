from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.publishing.x_client import (
    AmbiguousPublishError,
    OAuthCredentials,
    XAuthError,
    XClient,
    XNotConfiguredError,
    XPermissionError,
    XRateLimitedError,
    XValidationError,
    oauth1_authorization_header,
)

CREDS = OAuthCredentials(
    consumer_key="test-consumer-key",
    consumer_secret="test-consumer-secret",
    access_token="test-access-token",
    access_token_secret="test-access-token-secret",
)


def test_oauth1_signature_matches_x_documented_vector() -> None:
    header = oauth1_authorization_header(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
        CREDS,
        nonce="test-nonce",
        timestamp=1318622958,
        body_params={"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
    )
    assert 'oauth_signature="b3fymSuIMLDHuPh95HMoE4SHPRw%3D"' in header
    for key in ("oauth_consumer_key", "oauth_nonce", "oauth_token", "oauth_version"):
        assert key in header


def configured_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "_env_file": None,
        "x_publishing_mode": "official_api",
        "x_api_enabled": True,
        "x_consumer_key": SecretStr("ck"),
        "x_consumer_secret": SecretStr("cs"),
        "x_access_token": SecretStr("at"),
        "x_access_token_secret": SecretStr("ats"),
        "x_api_base_url": "https://api.x.test/2",
        "x_reads_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(handler) -> tuple[XClient, httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.x.test")
    return XClient(configured_settings(), client=http), http


async def test_create_post_uses_signed_json_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(201, json={"data": {"id": "12345", "text": "hello"}})

    client, http = make_client(handler)
    try:
        post = await client.create_post("hello world")
    finally:
        await http.aclose()
    assert post.id == "12345"
    assert post.url.endswith("/12345")
    assert str(seen["authorization"]).startswith("OAuth ")
    assert "oauth_signature" in str(seen["authorization"])
    assert seen["body"] == {"text": "hello world"}
    assert seen["path"] == "/2/tweets"


async def test_create_post_with_reply_target() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"data": {"id": "1", "text": "reply"}})

    client, http = make_client(handler)
    try:
        await client.create_post("reply", in_reply_to="99")
    finally:
        await http.aclose()
    assert captured["reply"] == {"in_reply_to_tweet_id": "99"}


async def test_rate_limit_surfaces_reset_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"x-rate-limit-reset": "1900000000"}, json={})

    client, http = make_client(handler)
    try:
        with pytest.raises(XRateLimitedError) as info:
            await client.create_post("hello")
    finally:
        await http.aclose()
    assert calls["n"] == 1
    assert info.value.retry_after is not None


@pytest.mark.parametrize("status,error", [(401, XAuthError), (403, XPermissionError), (400, XValidationError)])
async def test_http_errors_map_to_safe_exceptions(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    client, http = make_client(handler)
    try:
        with pytest.raises(error):
            await client.create_post("hello")
    finally:
        await http.aclose()


async def test_timeout_is_ambiguous_and_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    client, http = make_client(handler)
    try:
        with pytest.raises(AmbiguousPublishError):
            await client.create_post("hello")
    finally:
        await http.aclose()
    assert calls["n"] == 1


async def test_missing_credentials_fail_before_network() -> None:
    client = XClient(Settings(_env_file=None, x_publishing_mode="official_api", x_api_enabled=True))
    try:
        with pytest.raises(XNotConfiguredError):
            await client.create_post("hello")
    finally:
        await client.aclose()


async def test_get_mentions_parses_users_and_references() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2/users/1001/mentions"
        assert request.url.params["expansions"] == "author_id"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "55",
                        "text": "Does the scheduler help p99?",
                        "author_id": "42",
                        "conversation_id": "900",
                        "referenced_tweets": [{"type": "replied_to", "id": "77"}],
                    }
                ],
                "includes": {"users": [{"id": "42", "username": "devperson"}]},
            },
        )

    client, http = make_client(handler)
    try:
        mentions = await client.get_mentions("1001")
        me_handler = lambda request: httpx.Response(  # noqa: E731
            200, json={"data": {"id": "1001", "username": "techbrief", "name": "Tech"}}
        )
        me_client, me_http = make_client(me_handler)
        try:
            me = await me_client.get_me()
        finally:
            await me_http.aclose()
    finally:
        await http.aclose()
    assert mentions[0].id == "55"
    assert mentions[0].author_handle == "devperson"
    assert mentions[0].in_reply_to_post_id == "77"
    assert me["id"] == "1001"


async def test_metrics_require_reads_enabled_and_parse_public_metrics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ids"] == "1,2"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "public_metrics": {
                            "impression_count": 100,
                            "like_count": 5,
                            "reply_count": 1,
                            "retweet_count": 2,
                            "quote_count": 0,
                            "bookmark_count": 3,
                        },
                    }
                ]
            },
        )

    client, http = make_client(handler)
    try:
        metrics = await client.get_posts_metrics(["1", "2"])
    finally:
        await http.aclose()
    assert metrics["1"]["likes"] == 5
    assert metrics["1"]["bookmarks"] == 3

    disabled = XClient(configured_settings(x_reads_enabled=False),
                       client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(XPermissionError):
            await disabled.get_posts_metrics(["1"])
    finally:
        await disabled.aclose()
