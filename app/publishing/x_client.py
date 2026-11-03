"""Official X API v2 client with OAuth 1.0a user-context signing.

Design choices that matter for safety:
  * create-post requests are NEVER retried automatically: a timeout can mean the
    post was created, so the caller receives AmbiguousPublishError and must
    reconcile instead of risking a duplicate.
  * 429 responses surface the reset time; no unprompted sleeping/retrying loop.
  * credentials are only read from settings and never logged.
  * reads require x_reads_enabled; there is no scraping code path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

import httpx

from app.config import Settings
from app.logging_config import get_logger
from app.net import RetryPolicy, retry_async

logger = get_logger(__name__)


class XError(RuntimeError):
    """Base X API failure with a safe, non-secret message."""


class XNotConfiguredError(XError):
    pass


class XAuthError(XError):
    pass


class XPermissionError(XError):
    pass


class XValidationError(XError):
    pass


class XTransientError(XError):
    pass


class XRateLimitedError(XError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AmbiguousPublishError(XError):
    """The request may or may not have created a post. Never retry blindly."""


@dataclass(frozen=True)
class OAuthCredentials:
    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str


@dataclass(frozen=True)
class XMention:
    id: str
    text: str
    author_id: str | None
    author_handle: str | None
    conversation_id: str | None
    in_reply_to_post_id: str | None
    created_at: str | None


@dataclass(frozen=True)
class XPost:
    id: str
    text: str
    url: str
    raw: dict[str, Any]


def percent_encode(value: str) -> str:
    return quote(str(value), safe="~")


def oauth1_authorization_header(
    method: str,
    url: str,
    credentials: OAuthCredentials,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
    body_params: dict[str, str] | None = None,
) -> str:
    """Build an OAuth 1.0a HMAC-SHA1 Authorization header.

    ``body_params`` is used only for application/x-www-form-urlencoded requests;
    JSON bodies are not part of the OAuth signature base string.
    """
    parsed = urlparse(url)
    oauth_params: dict[str, str] = {
        "oauth_consumer_key": credentials.consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": credentials.access_token,
        "oauth_version": "1.0",
    }
    signature_params: list[tuple[str, str]] = list(oauth_params.items())
    signature_params.extend(parse_qsl(parsed.query, keep_blank_values=True))
    if body_params:
        signature_params.extend(body_params.items())
    normalized = "&".join(
        f"{percent_encode(key)}={percent_encode(value)}"
        for key, value in sorted(signature_params)
    )
    base_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    base_string = "&".join(
        [method.upper(), percent_encode(base_url), percent_encode(normalized)]
    )
    signing_key = (
        f"{percent_encode(credentials.consumer_secret)}"
        f"&{percent_encode(credentials.access_token_secret)}"
    )
    digest = hmac.new(
        signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1
    ).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(
        f'{percent_encode(key)}="{percent_encode(value)}"'
        for key, value in sorted(oauth_params.items())
    )


class XClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep=None,  # type: ignore[no-untyped-def]
    ) -> None:
        if settings.is_zero_cost_mode or not settings.x_api_enabled:
            raise RuntimeError(
                "XClient initialization is strictly prohibited in ZERO_COST_PUBLISHING_MODE. "
                "Ensure X_PUBLISHING_MODE='local_draft' and X_API_ENABLED=false."
            )
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.x_request_timeout_seconds),
            headers={"User-Agent": settings.http_user_agent},
        )
        self._owns_client = client is None
        self._sleep = sleep

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self.settings.x_write_credentials_present

    def _credentials(self) -> OAuthCredentials:
        s = self.settings
        if not s.x_write_credentials_present:
            raise XNotConfiguredError(
                "X OAuth 1.0a credentials are not configured"
            )
        return OAuthCredentials(
            consumer_key=s.x_consumer_key.get_secret_value(),  # type: ignore[union-attr]
            consumer_secret=s.x_consumer_secret.get_secret_value(),  # type: ignore[union-attr]
            access_token=s.x_access_token.get_secret_value(),  # type: ignore[union-attr]
            access_token_secret=s.x_access_token_secret.get_secret_value(),  # type: ignore[union-attr]
        )

    def _url(self, path: str) -> str:
        return f"{self.settings.x_api_base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        reset = response.headers.get("x-rate-limit-reset")
        if reset and reset.isdigit():
            return max(0.0, float(int(reset) - int(time.time())))
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                return None
        return None

    def _handle_error(self, response: httpx.Response, *, publish: bool) -> None:
        status = response.status_code
        if status == 429:
            raise XRateLimitedError("X API rate limit exceeded", self._retry_after(response))
        if status == 401:
            raise XAuthError("X rejected the OAuth credentials (401)")
        if status == 403:
            # 403 is used by X for both permission and some policy refusals.
            raise XPermissionError(
                "X rejected the request with HTTP 403; check app permissions and access tier"
            )
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("detail") or payload.get("title") or "")[:200]
        except ValueError:
            detail = response.text[:200]
        if status in {400, 422}:
            raise XValidationError(f"X rejected the request ({status}): {detail}")
        if status >= 500:
            if publish:
                raise AmbiguousPublishError(
                    f"X returned HTTP {status}; post creation is ambiguous and will not be retried"
                )
            raise XTransientError(f"X server error {status}")
        raise XError(f"X API error {status}: {detail}")

    # ------------------------------------------------------------------
    async def create_post(
        self, text: str, *, in_reply_to: str | None = None, quote: str | None = None
    ) -> XPost:
        credentials = self._credentials()
        url = self._url("/tweets")
        payload: dict[str, Any] = {"text": text}
        if in_reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": in_reply_to}
        if quote:
            payload["quote_tweet_id"] = quote
        headers = {
            "Authorization": oauth1_authorization_header("POST", url, credentials),
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AmbiguousPublishError(
                f"network failure during post creation ({type(exc).__name__}); "
                "the post may exist and must be reconciled before retrying"
            ) from exc
        if response.status_code >= 400:
            self._handle_error(response, publish=True)
        try:
            data = response.json()
            post_id = str(data["data"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AmbiguousPublishError(
                "X returned an unexpected response to post creation"
            ) from exc
        logger.info(
            "post created",
            extra={"extra_fields": {"x_post_id": post_id, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}},
        )
        return XPost(
            id=post_id,
            text=data.get("data", {}).get("text", text),
            url=f"https://x.com/i/web/status/{post_id}",
            raw={"id": post_id},
        )

    async def _read_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.x_reads_enabled:
            raise XPermissionError(
                "X reads are disabled (X_READS_ENABLED=false); research and mention ingestion skipped"
            )
        credentials = self._credentials()
        base_url = self._url(path)
        # OAuth 1.0a signs the query string, so the URL used for signing must be
        # exactly the URL sent (httpx must not add parameters afterwards).
        query = str(httpx.QueryParams(params))
        url = f"{base_url}?{query}" if query else base_url
        headers = {"Authorization": oauth1_authorization_header("GET", url, credentials)}

        async def _fetch() -> httpx.Response:
            return await self._client.get(url, headers=headers)

        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        response = await retry_async(
            _fetch,
            RetryPolicy(attempts=2, base_delay=1.0, jitter=0.2),
            retry_on=(httpx.TransportError,),
            **kwargs,
        )
        if response.status_code >= 400:
            self._handle_error(response, publish=False)
        try:
            return response.json()
        except ValueError:
            raise XError("X read response was not JSON") from None

    async def get_me(self) -> dict[str, str]:
        data = await self._read_get("/users/me", {"user.fields": "username,name"})
        payload = data.get("data") or {}
        if not payload.get("id"):
            raise XError("X did not return the authenticated user")
        return {
            "id": str(payload["id"]),
            "username": str(payload.get("username") or ""),
            "name": str(payload.get("name") or ""),
        }

    async def get_mentions(self, user_id: str, *, max_results: int = 10) -> list[XMention]:
        data = await self._read_get(
            f"/users/{user_id}/mentions",
            {
                "max_results": max(5, min(max_results, 100)),
                "tweet.fields": "author_id,conversation_id,created_at,referenced_tweets",
                "expansions": "author_id",
                "user.fields": "username",
            },
        )
        users = {
            str(user.get("id")): str(user.get("username") or "")
            for user in (data.get("includes") or {}).get("users", [])
        }
        mentions: list[XMention] = []
        for tweet in data.get("data", []) or []:
            author_id = str(tweet.get("author_id") or "") or None
            referenced = tweet.get("referenced_tweets") or []
            parent = next(
                (str(ref.get("id")) for ref in referenced if ref.get("type") == "replied_to"),
                None,
            )
            mentions.append(
                XMention(
                    id=str(tweet.get("id")),
                    text=str(tweet.get("text") or ""),
                    author_id=author_id,
                    author_handle=users.get(author_id or "") or None,
                    conversation_id=str(tweet.get("conversation_id") or "") or None,
                    in_reply_to_post_id=parent,
                    created_at=tweet.get("created_at"),
                )
            )
        return mentions

    async def get_tweets(self, tweet_ids: list[str]) -> list[XMention]:
        """Fetch multiple tweets with author and conversation metadata."""
        if not tweet_ids:
            return []
        data = await self._read_get(
            "/tweets",
            {
                "ids": ",".join(tweet_ids[:100]),
                "tweet.fields": "author_id,conversation_id,created_at,referenced_tweets",
                "expansions": "author_id",
                "user.fields": "username",
            },
        )
        users = {
            str(user.get("id")): str(user.get("username") or "")
            for user in (data.get("includes") or {}).get("users", [])
        }
        tweets: list[XMention] = []
        for tweet in data.get("data", []) or []:
            author_id = str(tweet.get("author_id") or "") or None
            referenced = tweet.get("referenced_tweets") or []
            parent = next(
                (str(ref.get("id")) for ref in referenced if ref.get("type") == "replied_to"),
                None,
            )
            tweets.append(
                XMention(
                    id=str(tweet.get("id")),
                    text=str(tweet.get("text") or ""),
                    author_id=author_id,
                    author_handle=users.get(author_id or "") or None,
                    conversation_id=str(tweet.get("conversation_id") or "") or None,
                    in_reply_to_post_id=parent,
                    created_at=tweet.get("created_at"),
                )
            )
        return tweets

    async def get_posts_metrics(self, post_ids: list[str]) -> dict[str, dict[str, int]]:
        if not post_ids:
            return {}
        payload = await self._read_get(
            "/tweets",
            {
                "ids": ",".join(post_ids[:100]),
                "tweet.fields": "public_metrics,created_at",
            },
        )
        data = payload.get("data", [])
        metrics: dict[str, dict[str, int]] = {}
        for item in data:
            public = item.get("public_metrics") or {}
            metrics[str(item.get("id"))] = {
                "impressions": int(public.get("impression_count", 0)),
                "likes": int(public.get("like_count", 0)),
                "replies": int(public.get("reply_count", 0)),
                "reposts": int(public.get("retweet_count", 0)),
                "quotes": int(public.get("quote_count", 0)),
                "bookmarks": int(public.get("bookmark_count", 0)),
            }
        return metrics

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
