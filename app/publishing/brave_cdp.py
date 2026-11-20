"""Brave browser publishing provider with explicit outcome confirmation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import DRAFT_NEEDS_RECONCILIATION, DRAFT_PUBLISHED, Draft
from app.memory.repositories import AuditRepository, PostRepository
from app.publishing.provider import PublishingProvider
from app.research.security import is_safe_url
from app.schemas.publishing import PolicyDecision, PublishResult

logger = get_logger(__name__)

DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


def parse_visible_count(value: str) -> int | None:
    """Parse an X UI count such as ``1,234``, ``12K`` or ``1.5M``."""
    cleaned = value.strip().replace(",", "").upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMB])?", cleaned)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {None: 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[
        match.group(2)
    ]
    return int(number * multiplier)


def metrics_from_aria_labels(labels: list[str]) -> dict[str, int]:
    """Extract public metrics from the accessible labels rendered by X."""
    metrics: dict[str, int] = {}
    names = {
        "view": "impressions",
        "like": "likes",
        "repl": "replies",
        "repost": "reposts",
        "quote": "quotes",
    }
    for label in labels:
        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KMB]?)\s+(views?|likes?|repl(?:y|ies)|reposts?|quotes?)\b", label, re.I)
        if not match:
            continue
        count = parse_visible_count(match.group(1))
        if count is None:
            continue
        lowered_name = match.group(2).lower()
        for prefix, key in names.items():
            if lowered_name.startswith(prefix):
                metrics[key] = max(metrics.get(key, 0), count)
                break
    return metrics


class PublishConfirmationError(RuntimeError):
    """The click happened, but X did not expose a verifiable status ID."""

KNOWN_BRAVE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    "/usr/bin/brave-browser",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def resolve_brave_executable() -> str | None:
    for candidate in KNOWN_BRAVE_PATHS:
        if os.path.isfile(candidate):
            return candidate
    for cmd in (
        "brave-browser",
        "brave",
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
    ):
        found = shutil.which(cmd)
        if found:
            return found
    return None


class BraveCDPPublisher(PublishingProvider):
    """Zero-cost hands-free publisher utilizing native Brave browser automation."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cdp_url: str = DEFAULT_CDP_ENDPOINT,
        profile_dir: str | None = None,
        clock=None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        # Restrict CDP connectivity strictly to explicitly permitted local loopback endpoints
        parsed_cdp = urlparse(cdp_url)
        cdp_host = (parsed_cdp.hostname or "").lower()
        if cdp_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Brave CDP endpoint must be a local loopback address: {cdp_url}")
        self.cdp_url = cdp_url
        self.profile_dir = profile_dir or str(Path.cwd() / ".brave_profile")
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _confirm_post_id(
        self,
        page: Any,
        text: str,
        *,
        excluded_ids: set[str] | None = None,
        include_replies: bool = False,
    ) -> str | None:
        """Confirm the posted text on the account profile and return its real X ID."""
        handle = self.settings.x_persona_handle.strip().lstrip("@")
        if not handle:
            return None
        excluded = excluded_ids or set()

        def _clean_text(s: str) -> str:
            # Strip URLs and collapse whitespace for resilient matching on quote-tweets/replies
            no_urls = re.sub(r"https?://\S+", "", s)
            return " ".join(no_urls.split()).strip()

        expected_clean = _clean_text(text)
        expected_raw = " ".join(text.split())

        urls_to_try = (
            [f"https://x.com/{handle}/with_replies", f"https://x.com/{handle}"]
            if include_replies
            else [f"https://x.com/{handle}", f"https://x.com/{handle}/with_replies"]
        )

        for attempt in range(3):
            profile_url = urls_to_try[attempt % len(urls_to_try)]
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=35000)
            articles = page.locator('article[data-testid="tweet"]')
            try:
                await articles.first.wait_for(state="visible", timeout=10000)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2.0)
                continue
            for index in range(min(await articles.count(), 8)):
                article = articles.nth(index)
                text_node = article.locator('[data-testid="tweetText"]').first
                if await text_node.count() == 0:
                    continue
                raw_inner = await text_node.inner_text()
                visible_raw = " ".join(raw_inner.split())
                visible_clean = _clean_text(raw_inner)

                matches = (
                    visible_raw == expected_raw
                    or (expected_clean and expected_clean in visible_clean)
                    or (visible_clean and visible_clean in expected_clean)
                )
                if not matches:
                    continue
                links = article.locator(f'a[href*="/{handle}/status/"]')
                for link_index in range(await links.count()):
                    href = await links.nth(link_index).get_attribute("href") or ""
                    match = re.search(r"/status/(\d+)", href)
                    if match and match.group(1) not in excluded:
                        return match.group(1)
            if attempt < 2:
                await asyncio.sleep(2.0)
        return None

    async def _mark_needs_reconciliation(self, draft: Draft, reason: str) -> None:
        async with self.session_factory() as session:
            db_draft = await session.get(Draft, draft.id)
            if db_draft is not None:
                db_draft.status = DRAFT_NEEDS_RECONCILIATION
                db_draft.error = reason[:4000]
            await AuditRepository.log(
                session,
                action="publish_brave_cdp",
                outcome="unknown",
                account_id=draft.account_id,
                details={"draft_id": draft.id, "reason": reason},
            )
            await session.commit()

    async def is_cdp_available(self) -> bool:
        """Check if local browser CDP port is open and responding.

        Note: The connection to the local browser CDP daemon is plain HTTP over
        the local loopback interface (http://127.0.0.1:9222). TLS verification is
        not applicable to plain HTTP local loopback connections. Insecure remote
        HTTPS connections are strictly prohibited by endpoint validation above.
        """
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.cdp_url}/json/version")
                return resp.status_code == 200
        except Exception:
            return False

    async def validate_connection(self) -> dict[str, Any]:
        """Validate if Brave browser executable and auth credentials are ready."""
        brave_exe = resolve_brave_executable()
        has_auth_token = bool(self.settings.x_auth_token)
        has_ct0 = bool(self.settings.x_ct0)

        if not brave_exe and not await self.is_cdp_available():
            return {
                "valid": False,
                "provider": "brave_cdp",
                "detail": "Brave browser executable not found. Ensure Brave is installed.",
            }

        if not has_auth_token or not has_ct0:
            return {
                "valid": False,
                "provider": "brave_cdp",
                "detail": "X_AUTH_TOKEN and X_CT0 must be provided in .env for authenticated publishing.",
            }

        return {
            "valid": True,
            "provider": "brave_cdp",
            "detail": f"Brave browser publishing ready ({brave_exe}); every post requires confirmation.",
        }

    async def get_status(self) -> dict[str, Any]:
        brave_exe = resolve_brave_executable()
        has_tokens = bool(self.settings.x_auth_token and self.settings.x_ct0)
        cdp_active = await self.is_cdp_available()
        return {
            "provider": "brave_cdp",
            "zero_cost": True,
            "configured": bool(brave_exe or cdp_active) and has_tokens,
            "brave_installed": bool(brave_exe),
            "brave_path": brave_exe,
            "cdp_active": cdp_active,
            "tokens_present": has_tokens,
            "cost_inr": 0.0,
            "cost_usd": 0.0,
            "outbound_requests_allowed": self.settings.publishing_enabled
            and not self.settings.dry_run,
            "description": "Brave Browser Native Automation (₹0 Zero-Cost, Anti-Detection)",
        }

    async def _get_browser_context(self, p: Any):
        """Connect over active CDP if available, otherwise launch isolated Brave context."""
        if await self.is_cdp_available():
            logger.info(
                "Connecting to existing Brave CDP session",
                extra={"extra_fields": {"cdp_url": self.cdp_url}},
            )
            browser = await p.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            return browser, context, False

        brave_exe = resolve_brave_executable()
        if not brave_exe:
            raise RuntimeError("Brave browser executable could not be found.")

        os.makedirs(self.profile_dir, exist_ok=True)
        logger.info(
            "Launching Brave persistent browser context",
            extra={"extra_fields": {"executable": brave_exe, "profile": self.profile_dir}},
        )

        is_headless = False
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ]
        if os.name != "nt" and "DISPLAY" not in os.environ:
            is_headless = True
            launch_args.extend(["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"])
        else:
            launch_args.extend(["--window-position=-2000,-2000", "--window-size=1280,800"])

        context = await p.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=brave_exe,
            headless=is_headless,
            args=launch_args,
        )

        # Inject session cookies if available
        auth_token = (
            self.settings.x_auth_token.get_secret_value() if self.settings.x_auth_token else None
        )
        ct0 = self.settings.x_ct0.get_secret_value() if self.settings.x_ct0 else None

        if auth_token and ct0:
            await context.add_cookies(
                [
                    {
                        "name": "auth_token",
                        "value": auth_token,
                        "domain": ".x.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                    },
                    {
                        "name": "ct0",
                        "value": ct0,
                        "domain": ".x.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                    },
                ]
            )

        return None, context, True

    async def _download_temp_media(self, image_url: str) -> Path | None:
        """Download remote image to local temporary file for browser upload."""
        # Validate that remote media URL is safe (anti-SSRF) and use strict TLS verification
        if not is_safe_url(image_url):
            logger.warning(
                f"Rejected unsafe media URL for download: {image_url}",
                extra={"extra_fields": {"url": image_url}},
            )
            return None

        try:
            temp_dir = Path(".temp_media")
            temp_dir.mkdir(exist_ok=True)  # noqa: ASYNC240
            ext = ".jpg"
            lowered = image_url.lower()
            if ".png" in lowered:
                ext = ".png"
            elif ".webp" in lowered:
                ext = ".webp"
            file_path = (
                temp_dir / f"x_media_{int(datetime.now(UTC).timestamp())}_{os.getpid()}{ext}"
            )

            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=True, verify=True
            ) as client:
                resp = await client.get(
                    image_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    },
                )
                if resp.status_code == 200 and len(resp.content) > 1500:
                    file_path.write_bytes(resp.content)
                    return file_path
        except Exception as exc:
            logger.warning(
                f"Failed to download image for post ({exc}), will dispatch text-only",
                extra={"extra_fields": {"url": image_url, "error": str(exc)}},
            )
        return None

    async def publish(
        self,
        draft: Draft,
        parts: list[str],
        *,
        decision: PolicyDecision | None = None,
    ) -> PublishResult:
        """Publish a post or thread directly through Brave browser."""
        if not self.settings.publishing_enabled or self.settings.dry_run:
            logger.info("Publishing skipped: simulated / dry run")
            return PublishResult(
                status="simulated",
                draft_id=draft.id,
                post_ids=[],
                reasons=["dry_run active"],
                policy=decision,
            )

        brave_exe = resolve_brave_executable()
        if not brave_exe and not await self.is_cdp_available():
            msg = "Cannot publish: Brave browser is not installed or detected."
            logger.error(msg)
            return PublishResult(
                status="failed",
                draft_id=draft.id,
                post_ids=[],
                reasons=[msg],
                policy=decision,
            )

        post_ids: list[str] = []
        now = self.clock()
        reply_parent = draft.in_reply_to_post_id if draft.kind == "reply" else None

        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)

            try:
                # Iterate through thread parts
                for position, text_content in enumerate(parts):
                    page = await context.new_page()
                    temp_image: Path | None = None
                    try:
                        # Extract image_url for position 0 (main post)
                        if position == 0:
                            img_url = None
                            if draft.policy_report and isinstance(draft.policy_report, dict):
                                img_url = draft.policy_report.get("image_url")
                            if (
                                not img_url
                                and draft.verification_report
                                and isinstance(draft.verification_report, dict)
                            ):
                                img_url = draft.verification_report.get("image_url")
                            if img_url:
                                temp_image = await self._download_temp_media(img_url)
                                if temp_image is None:
                                    raise RuntimeError(
                                        "selected editorial image could not be downloaded; post held"
                                    )

                        encoded_text = quote(text_content)
                        intent_url = f"https://x.com/intent/post?text={encoded_text}"

                        # If this is a reply draft, attach reply metadata
                        if reply_parent:
                            intent_url += f"&in_reply_to={reply_parent}"

                        # If this is a quote tweet, attach quote URL
                        quote_url = None
                        if draft.policy_report and isinstance(draft.policy_report, dict):
                            quote_url = draft.policy_report.get("quoted_post_url")
                        if not quote_url:
                            quote_url = getattr(draft, "quoted_post_url", None)
                        if quote_url and position == 0:
                            intent_url += f"&url={quote(str(quote_url))}"

                        logger.info(
                            "Navigating to X intent compose URL",
                            extra={"extra_fields": {"position": position, "draft_id": draft.id}},
                        )
                        await page.goto(intent_url, wait_until="domcontentloaded", timeout=35000)
                        await asyncio.sleep(2.5)

                        # If we have a verified editorial image to attach, upload via Playwright file input
                        if temp_image and temp_image.exists():
                            try:
                                file_input = page.locator(
                                    'input[data-testid="fileInput"], input[type="file"][accept*="image"]'
                                )
                                if await file_input.count() == 0:
                                    with contextlib.suppress(Exception):
                                        await file_input.first.wait_for(
                                            state="attached", timeout=4000
                                        )
                                if await file_input.count() > 0:
                                    logger.info(
                                        "Attaching editorial image via Brave file input",
                                        extra={"extra_fields": {"image_path": str(temp_image)}},
                                    )
                                    await file_input.first.set_input_files(
                                        str(temp_image.resolve())
                                    )
                                    await page.locator(
                                        '[data-testid="attachments"], [data-testid="mediaAttachment"]'
                                    ).wait_for(state="visible", timeout=12000)
                                    logger.info(
                                        "Image attachment preview confirmed visible on X compose dialog"
                                    )
                                else:
                                    raise RuntimeError(
                                        "X compose did not expose a media file input; post held"
                                    )
                            except Exception as img_err:
                                raise RuntimeError(
                                    f"selected editorial image was not attached: {img_err}"
                                ) from img_err

                        # Locate Tweet / Post button
                        post_button = page.locator(
                            '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
                        ).first
                        await post_button.wait_for(state="visible", timeout=15000)

                        # Check if disabled, wait briefly for text hydration
                        if await post_button.is_disabled():
                            await asyncio.sleep(1.5)

                        logger.info("Dispatching post via Brave click action")
                        await post_button.click()
                        await asyncio.sleep(4.0)

                        post_id = await self._confirm_post_id(
                            page,
                            text_content,
                            excluded_ids={*post_ids, *(filter(None, [draft.in_reply_to_post_id]))},
                            include_replies=bool(reply_parent),
                        )
                        if post_id is None:
                            raise PublishConfirmationError(
                                "X did not expose a matching status ID after the dispatch click"
                            )
                        post_ids.append(post_id)
                        reply_parent = post_id

                        # Record in PostRepository
                        async with self.session_factory() as session:
                            await PostRepository.create(
                                session,
                                account_id=draft.account_id,
                                x_post_id=post_id,
                                text=text_content,
                                kind=draft.kind,
                                draft_id=draft.id,
                                position=position,
                                published_at=now,
                            )
                            await session.commit()

                    except PublishConfirmationError as exc:
                        reason = str(exc)
                        await self._mark_needs_reconciliation(draft, reason)
                        return PublishResult(
                            status="unknown",
                            draft_id=draft.id,
                            post_ids=post_ids,
                            reasons=[reason],
                            policy=decision,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Error during Brave post dispatch",
                            extra={"extra_fields": {"error": str(exc)}},
                        )
                        return PublishResult(
                            status="failed",
                            draft_id=draft.id,
                            post_ids=post_ids,
                            reasons=[f"Brave browser interaction error: {exc}"],
                            policy=decision,
                        )
                    finally:
                        if temp_image and temp_image.exists():
                            with contextlib.suppress(Exception):
                                temp_image.unlink()
                        with contextlib.suppress(Exception):
                            await page.close()

            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()

        # Update draft in database to published
        async with self.session_factory() as session:
            db_draft = await session.get(Draft, draft.id)
            if db_draft:
                db_draft.status = DRAFT_PUBLISHED
                db_draft.published_at = now
                policy = dict(db_draft.policy_report or {})
                policy["published_via"] = "brave_cdp"
                policy["zero_cost"] = True
                db_draft.policy_report = policy

            await AuditRepository.log(
                session,
                action="publish_brave_cdp",
                outcome="published",
                account_id=draft.account_id,
                details={
                    "draft_id": draft.id,
                    "parts": len(parts),
                    "zero_cost": True,
                    "provider": "brave_cdp",
                },
            )
            await session.commit()

        logger.info(
            "Draft successfully published via Brave automation",
            extra={"extra_fields": {"draft_id": draft.id, "post_ids": post_ids}},
        )
        return PublishResult(
            status="published",
            draft_id=draft.id,
            post_ids=post_ids,
            policy=decision,
        )

    async def repost(self, target_post_url: str) -> dict[str, Any]:
        """Repost (retweet) a tweet directly via Brave browser."""
        if not self.settings.publishing_enabled or self.settings.dry_run:
            logger.info(
                "Repost skipped: dry_run active", extra={"extra_fields": {"url": target_post_url}}
            )
            return {"status": "simulated", "url": target_post_url, "reason": "dry_run active"}

        brave_exe = resolve_brave_executable()
        if not brave_exe and not await self.is_cdp_available():
            return {"status": "failed", "url": target_post_url, "reason": "Brave not available"}

        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)
            try:
                page = await context.new_page()
                try:
                    await page.goto(target_post_url, wait_until="domcontentloaded", timeout=35000)
                    await asyncio.sleep(2.5)

                    retweet_btn = page.locator('[data-testid="retweet"]').first
                    await retweet_btn.wait_for(state="visible", timeout=12000)
                    await retweet_btn.click()
                    await asyncio.sleep(1.0)

                    confirm_btn = page.locator('[data-testid="retweetConfirm"]').first
                    await confirm_btn.wait_for(state="visible", timeout=8000)
                    await confirm_btn.click()
                    await asyncio.sleep(2.5)
                    return {"status": "reposted", "url": target_post_url}
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()

    async def like(self, target_post_url: str) -> dict[str, Any]:
        """Like a target post via Brave browser (natural human behavior)."""
        if not self.settings.publishing_enabled or self.settings.dry_run:
            return {"status": "simulated", "url": target_post_url, "action": "like"}

        brave_exe = resolve_brave_executable()
        if not brave_exe and not await self.is_cdp_available():
            return {"status": "failed", "url": target_post_url, "reason": "Brave not available"}

        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)
            try:
                page = await context.new_page()
                try:
                    await page.goto(target_post_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2.0)
                    like_btn = page.locator('[data-testid="like"]').first
                    if await like_btn.count() > 0:
                        await like_btn.click()
                        await asyncio.sleep(1.0)
                        return {"status": "liked", "url": target_post_url}
                    return {"status": "already_liked_or_missing", "url": target_post_url}
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()

    async def fetch_tweet_details(self, tweet_url: str) -> dict[str, Any]:
        """Fetch author and text of a tweet via Brave."""
        if not await self.is_cdp_available() and not resolve_brave_executable():
            return {"url": tweet_url, "author": "", "text": "", "tweet_id": ""}
        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)
            try:
                page = await context.new_page()
                try:
                    await page.goto(tweet_url, wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(2.0)
                    tweet_article = page.locator('article[data-testid="tweet"]').first
                    await tweet_article.wait_for(state="visible", timeout=8000)
                    text = await tweet_article.locator(
                        '[data-testid="tweetText"]'
                    ).first.inner_text()
                    user_name_elem = tweet_article.locator('[data-testid="User-Name"]').first
                    author_text = (
                        await user_name_elem.inner_text()
                        if await user_name_elem.count() > 0
                        else ""
                    )
                    match = re.search(r"@(\w+)", author_text)
                    author = match.group(1) if match else ""
                    match_id = re.search(r"/status/(\d+)", tweet_url)
                    tweet_id = match_id.group(1) if match_id else ""
                    return {"url": tweet_url, "author": author, "text": text, "tweet_id": tweet_id}
                except Exception as exc:
                    logger.warning(
                        "Failed to scrape tweet details (tweet may be 404 or protected)",
                        extra={"extra_fields": {"url": tweet_url, "error": str(exc)}},
                    )
                    return {"url": tweet_url, "author": "", "text": "", "tweet_id": ""}
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()

    async def publish_with_video(
        self,
        draft: Draft,
        caption: str,
        video_path: Path,
    ) -> PublishResult:
        """Publish a tweet with an attached local video file via Brave CDP.

        Uses the X compose intent URL with text pre-filled, then attaches the
        video file via the hidden file input, waits for processing, and clicks
        Post. Refuses a text-only fallback if video upload fails.
        """
        if not self.settings.publishing_enabled or self.settings.dry_run:
            logger.info("Video publish skipped: dry_run active")
            return PublishResult(
                status="simulated",
                draft_id=draft.id,
                post_ids=[],
                reasons=["dry_run active"],
            )

        brave_exe = resolve_brave_executable()
        if not brave_exe and not await self.is_cdp_available():
            return PublishResult(
                status="failed",
                draft_id=draft.id,
                post_ids=[],
                reasons=["Brave not available"],
            )

        post_ids: list[str] = []
        now = self.clock()
        encoded_text = quote(caption)
        intent_url = f"https://x.com/intent/post?text={encoded_text}"

        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)
            try:
                page = await context.new_page()
                try:
                    await page.goto(intent_url, wait_until="domcontentloaded", timeout=35_000)
                    await asyncio.sleep(2.5)

                    # Attach video via file input
                    video_attached = False
                    video_valid = await asyncio.to_thread(
                        lambda: video_path.exists() and video_path.stat().st_size > 1000
                    )
                    if video_valid:
                        resolved_path = await asyncio.to_thread(lambda: str(video_path.resolve()))
                        try:
                            file_input = page.locator(
                                'input[data-testid="fileInput"], input[type="file"]'
                            ).first
                            if await file_input.count() == 0:
                                with contextlib.suppress(Exception):
                                    await file_input.wait_for(state="attached", timeout=5_000)

                            if await file_input.count() > 0:
                                await file_input.set_input_files(resolved_path)
                                # Wait for X to process the video upload (can take 5-20s)
                                logger.info(
                                    "Video file attached; waiting for X to process upload",
                                    extra={"extra_fields": {"video": str(video_path)}},
                                )
                                await asyncio.sleep(8.0)
                                # Confirm attachment preview appeared
                                attachment = page.locator(
                                    '[data-testid="attachments"], [data-testid="mediaAttachment"]'
                                )
                                try:
                                    await attachment.wait_for(state="visible", timeout=20_000)
                                    video_attached = True
                                    logger.info("Video attachment confirmed visible")
                                except Exception as attach_err:
                                    raise RuntimeError(
                                        f"video attachment preview not confirmed: {attach_err}"
                                    ) from attach_err
                        except Exception as vid_err:
                            raise RuntimeError(f"video file attachment failed: {vid_err}") from vid_err

                    if not video_attached:
                        raise RuntimeError("video was not attached; refusing text-only fallback")

                    # Wait for Post button to be enabled (X processes video upload in background)
                    post_button = page.locator(
                        '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
                    ).first
                    await post_button.wait_for(state="visible", timeout=20_000)
                    for _ in range(15):
                        if not await post_button.is_disabled():
                            break
                        await asyncio.sleep(2.0)

                    await post_button.click()
                    await asyncio.sleep(5.0)

                    post_id = await self._confirm_post_id(
                        page,
                        caption,
                        excluded_ids=set(),
                        include_replies=False,
                    )
                    if post_id is None:
                        raise PublishConfirmationError(
                            "X did not expose a matching status ID after video post"
                        )
                    post_ids.append(post_id)

                    async with self.session_factory() as session:
                        await PostRepository.create(
                            session,
                            account_id=draft.account_id,
                            x_post_id=post_id,
                            text=caption,
                            kind="original",
                            draft_id=draft.id,
                            position=0,
                            published_at=now,
                        )
                        await session.commit()

                    logger.info(
                        "Video post published via Brave",
                        extra={
                            "extra_fields": {
                                "post_id": post_id,
                                "video_attached": video_attached,
                                "draft_id": draft.id,
                            }
                        },
                    )
                    return PublishResult(
                        status="published",
                        draft_id=draft.id,
                        post_ids=post_ids,
                    )

                except PublishConfirmationError as exc:
                    reason = str(exc)
                    await self._mark_needs_reconciliation(draft, reason)
                    return PublishResult(
                        status="unknown",
                        draft_id=draft.id,
                        post_ids=post_ids,
                        reasons=[reason],
                    )
                except Exception as exc:
                    logger.exception(
                        "Error during video post dispatch",
                        extra={"extra_fields": {"error": str(exc)}},
                    )
                    return PublishResult(
                        status="failed",
                        draft_id=draft.id,
                        post_ids=post_ids,
                        reasons=[f"Video post error: {exc}"],
                    )
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()

    async def scan_profile_tweets(self, handle: str, limit: int = 3) -> list[dict[str, Any]]:
        """Scan latest tweets from a profile handle via Brave."""
        clean_handle = handle.strip().lstrip("@")
        profile_url = f"https://x.com/{clean_handle}"
        if not await self.is_cdp_available() and not resolve_brave_executable():
            return []
        async with async_playwright() as p:
            browser, context, is_owned_context = await self._get_browser_context(p)
            try:
                page = await context.new_page()
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=35000)
                    articles = page.locator('article[data-testid="tweet"]')
                    try:
                        await articles.first.wait_for(state="visible", timeout=12000)
                    except Exception:
                        # If first tweet isn't immediately visible, dismiss any modal and scroll
                        for close_sel in [
                            '[data-testid="app-bar-close"]',
                            'button[aria-label="Close"]',
                            '[data-testid="sheet:close"]',
                        ]:
                            btn = page.locator(close_sel).first
                            if await btn.count() > 0:
                                with contextlib.suppress(Exception):
                                    await btn.click()
                                    await asyncio.sleep(0.5)

                    await page.evaluate("window.scrollBy(0, 300)")
                    await asyncio.sleep(1.0)

                    tweets = []
                    count = min(await articles.count(), limit)
                    for i in range(count):
                        article = articles.nth(i)
                        try:
                            text_elem = article.locator('[data-testid="tweetText"]').first
                            text = (
                                await text_elem.inner_text() if await text_elem.count() > 0 else ""
                            )

                            # Find status link with multiple selector strategies
                            href = ""
                            status_links = article.locator('a[href*="/status/"]')
                            num_links = await status_links.count()
                            for k in range(num_links):
                                candidate = await status_links.nth(k).get_attribute("href") or ""
                                candidate = candidate.split("?")[0]
                                if re.search(r"/[^/]+/status/\d+$", candidate):
                                    href = candidate
                                    break

                            if not href and num_links > 0:
                                href = (await status_links.first.get_attribute("href") or "").split("?")[0]

                            tweet_url = (
                                f"https://x.com{href}" if href and href.startswith("/") else href
                            )
                            match_id = re.search(r"/status/(\d+)", tweet_url)
                            tweet_id = match_id.group(1) if match_id else ""
                            if tweet_url and text:
                                labels = await article.locator("[aria-label]").evaluate_all(
                                    "els => els.map(el => el.getAttribute('aria-label') || '')"
                                )
                                tweets.append(
                                    {
                                        "url": tweet_url,
                                        "tweet_id": tweet_id,
                                        "author": clean_handle,
                                        "text": text,
                                        **metrics_from_aria_labels(labels),
                                    }
                                )
                        except Exception:
                            continue
                    return tweets
                except Exception as exc:
                    logger.warning(
                        "Failed to scan profile tweets (profile may be private or rate limited)",
                        extra={"extra_fields": {"handle": clean_handle, "error": str(exc)}},
                    )
                    return []
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            finally:
                if is_owned_context:
                    with contextlib.suppress(Exception):
                        await context.close()
                elif browser:
                    with contextlib.suppress(Exception):
                        await browser.close()
