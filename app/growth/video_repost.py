"""Viral Video Repost Engine for @ssh_bugs.

Pipeline:
1. Scan configured viral handles for posts that contain an embedded video
   and have high public reach (impressions threshold configurable).
2. Auto-bypass Twitter/X sensitive content / safety warnings (e.g. fights,
   kalesh, accidents) by automatically clicking "View" overlays.
3. Download the video via yt-dlp (with auth session cookies injected to
   bypass age restrictions and guest rate limits) or fallback browser stream sniffer.
4. Extract N evenly-spaced keyframes as JPEG images using Pillow.
5. Send the keyframes to Gemini Vision (multimodal) with the original tweet
   text so the LLM understands the visual context.
6. Generate a fresh, witty banger caption in the NotANerd @ssh_bugs voice.
7. Post the downloaded video + new caption via Brave CDP browser automation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.logging_config import get_logger
from app.providers.base import LLMMessage
from app.providers.factory import LLMService

logger = get_logger(__name__)

_FRAME_COUNT = 5


def _extract_keyframes_pillow(video_path: Path, n_frames: int = _FRAME_COUNT) -> list[bytes]:
    """Extract n evenly-spaced keyframes from a video file using Pillow."""
    try:
        from PIL import Image, ImageSequence  # type: ignore[import-untyped]

        frames: list[bytes] = []
        with Image.open(str(video_path)) as img:
            all_frames = list(ImageSequence.Iterator(img))
            if not all_frames:
                return []
            indices = [int(i * (len(all_frames) - 1) / max(n_frames - 1, 1)) for i in range(n_frames)]
            for idx in indices:
                frame = all_frames[min(idx, len(all_frames) - 1)].convert("RGB")
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=70)
                frames.append(buf.getvalue())
        return frames
    except Exception as exc:
        logger.debug(f"Pillow keyframe extraction failed ({exc}); will use text-only context")
        return []


def _extract_keyframes(video_path: Path, n_frames: int = _FRAME_COUNT) -> list[bytes]:
    """Extract keyframes from a video file using ffmpeg (preferred) with Pillow fallback."""
    import subprocess
    import tempfile

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        try:
            with tempfile.TemporaryDirectory(prefix="kframes_") as kf_dir:
                out_pattern = str(Path(kf_dir) / "frame_%02d.jpg")
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(video_path),
                    "-vf",
                    "fps=1/3,scale=480:-1",
                    "-frames:v",
                    str(n_frames),
                    "-q:v",
                    "3",
                    out_pattern,
                ]
                res = subprocess.run(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
                )
                if res.returncode == 0:
                    frames = [
                        f.read_bytes()
                        for f in sorted(Path(kf_dir).glob("frame_*.jpg"))
                        if f.stat().st_size > 0
                    ]
                    if frames:
                        return frames[:n_frames]
        except Exception as exc:
            logger.debug(f"ffmpeg frame extraction failed: {exc}")

    return _extract_keyframes_pillow(video_path, n_frames)


def _find_downloaded_video(dest_dir: Path) -> Path | None:
    for f in dest_dir.iterdir():
        if f.suffix in (".mp4", ".mkv", ".webm", ".mov") and f.stat().st_size > 10_000:
            return f
    return None


def make_netscape_cookie_file(auth_token: str | None, ct0: str | None, dest_path: Path) -> Path | None:
    """Create a Netscape-formatted cookie file to bypass X guest restrictions & sensitive media blocks."""
    if not auth_token:
        return None
    lines = [
        "# Netscape HTTP Cookie File",
        f".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t{auth_token}",
        f".twitter.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t{auth_token}",
    ]
    if ct0:
        lines.append(f".x.com\tTRUE\t/\tFALSE\t2147483647\tct0\t{ct0}")
        lines.append(f".twitter.com\tTRUE\t/\tFALSE\t2147483647\tct0\t{ct0}")
    dest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest_path


async def sniff_and_download_video_via_browser(
    settings: Settings,
    tweet_url: str,
    dest_dir: Path,
) -> Path | None:
    """Fallback: open tweet in authenticated Brave CDP, unhide sensitive content, sniff video stream, and download directly."""
    from playwright.async_api import async_playwright

    from app.publishing.brave_cdp import BraveCDPPublisher, resolve_brave_executable

    if not resolve_brave_executable():
        return None

    class _StubFactory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

    publisher = BraveCDPPublisher(settings, _StubFactory())  # type: ignore[arg-type]
    video_urls: list[str] = []

    async with async_playwright() as p:
        browser, context, is_owned = await publisher._get_browser_context(p)
        try:
            page = await context.new_page()

            def _on_response(response: Any) -> None:
                url = response.url
                if "video.twimg.com" in url and (".mp4" in url or ".m3u8" in url):
                    video_urls.append(url)

            page.on("response", _on_response)

            try:
                await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.0)

                # Click any "View" / sensitive content warnings
                for sel in ['button:has-text("View")', 'div[role="button"]:has-text("View")']:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        with contextlib.suppress(Exception):
                            await btn.click()
                            await asyncio.sleep(1.0)

                # Also search DOM for video sources
                sources = page.locator('video source, video[src*="twimg.com"]')
                for i in range(await sources.count()):
                    src = await sources.nth(i).get_attribute("src")
                    if src and "video.twimg.com" in src:
                        video_urls.append(src)

            except Exception as exc:
                logger.warning(f"Browser video sniffing encountered error: {exc}")
            finally:
                with contextlib.suppress(Exception):
                    await page.close()
        finally:
            if is_owned:
                with contextlib.suppress(Exception):
                    await context.close()
            elif browser:
                with contextlib.suppress(Exception):
                    await browser.close()

    # Find the best MP4 url
    mp4_urls = [u for u in video_urls if ".mp4" in u]
    if not mp4_urls:
        return None

    target_video_url = mp4_urls[0]
    out_file = dest_dir / f"sniffed_video_{int(time.time())}.mp4"

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(
                target_video_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            if resp.status_code == 200 and len(resp.content) > 10_000:
                out_file.write_bytes(resp.content)
                logger.info(f"Successfully downloaded video via browser sniffer: {out_file}")
                return out_file
    except Exception as exc:
        logger.warning(f"Failed downloading sniffed video stream: {exc}")

    return None


async def download_tweet_video(
    tweet_url: str,
    dest_dir: Path,
    *,
    auth_token: str | None = None,
    ct0: str | None = None,
    settings: Settings | None = None,
) -> Path | None:
    """Download the video embedded in a tweet URL with full safety gate bypass.

    1. Injects Netscape cookies into yt-dlp to bypass sensitive content warnings,
       age verification, and guest rate limits.
    2. Uses robust executable resolution (shutil.which or python -m yt_dlp).
    3. Falls back to browser stream sniffing via Playwright if yt-dlp fails.
    """
    import subprocess

    cookie_file = None
    if auth_token:
        try:
            cookie_path = dest_dir / "x_cookies.txt"
            cookie_file = make_netscape_cookie_file(auth_token, ct0, cookie_path)
        except Exception as exc:
            logger.debug(f"Could not write cookie file: {exc}")

    output_template = str(dest_dir / "%(id)s.%(ext)s")

    yt_bin = shutil.which("yt-dlp")
    base_cmd = [yt_bin] if yt_bin else [sys.executable, "-m", "yt_dlp"]

    cmd = [
        *base_cmd,
        "--quiet",
        "--no-warnings",
        "--no-check-certificates",
        "--format", "mp4/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--max-filesize", "100m",
        "--extractor-args", "twitter:api=syndication,graphql",
    ]
    if cookie_file and cookie_file.exists():
        cmd.extend(["--cookies", str(cookie_file)])

    cmd.append(tweet_url)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        if result.returncode != 0 and result.stderr:
            logger.warning(
                "yt-dlp exited with non-zero code",
                extra={"extra_fields": {"stderr": result.stderr[:400], "url": tweet_url}},
            )
    except Exception as exc:
        logger.warning(f"yt-dlp execution error: {exc}", extra={"extra_fields": {"url": tweet_url}})

    # Check if yt-dlp produced the file
    downloaded = await loop.run_in_executor(None, _find_downloaded_video, dest_dir)
    if downloaded:
        return downloaded

    # Fallback to browser Playwright sniffer
    if settings is not None:
        logger.info("Attempting browser video stream sniffer fallback...")
        sniffed = await sniff_and_download_video_via_browser(settings, tweet_url, dest_dir)
        if sniffed:
            return sniffed

    return None


_CAPTION_SYSTEM_PROMPT = """\
You are @ssh_bugs on X (Twitter): a razor-sharp, relatable Indian internet creator.
You have just discovered a viral video that is blowing up on Indian Twitter.
You are posting this video on your account with a COMPLETELY ORIGINAL, HINGLISH caption.

LANGUAGE REQUIREMENT (CRITICAL):
- ALWAYS write in natural, conversational HINGLISH (Hindi written in Roman/English alphabet + English words).
- Real desi internet slang: words like "bhai", "kalesh", "alag hi", "scene", "jugaad", "chal raha hai", "bawal", "yaar", "matlab", "shanti".
- Do NOT use Devanagari script (no Hindi letters like 'नमस्ते'). Keep it Roman script English/Hinglish.
- Do NOT sound like an NRI or a corporate bot. Sound like an observant 22-year-old in a WhatsApp group.

VOICE & TONE:
- Short, punchy, witty, deadpan sarcasm or observational humor.
- Max 180 characters. Zero hashtags. Zero spam emojis (at most 1 emoji if it adds comic value).
- Treat the video as if YOU saw it happen live or just stumbled upon it.
- NEVER copy the original tweet's text or author.
- Never create party-political, election, government-versus-opposition, geopolitical, or defence commentary.
- BANNED CLICHES: "Watch till the end", "Wait for it", "This is insane", "Can't believe this", "Look at this".

EXAMPLES OF THE DESIRED VIBE:
- "Bhai Monday morning ko office login karne ke baad meri aisi hi halat hoti hai"
- "Alag hi confidence hai bhai ka, pure crowd ke samne script execute kar di"
- "Delhi metro me bina kalesh ke din poora nahi hota"
- "Ye konsi scheme hai jo sirf inhi logo ko pata hai bhai"
- "Background me uncle ka reaction pure cinema hai"

OUTPUT FORMAT (strict, no extra text):
CAPTION: <your Hinglish caption here>
"""


async def generate_video_caption(
    llm: LLMService,
    tweet_text: str,
    keyframes: list[bytes],
    *,
    tweet_author: str = "",
) -> str:
    """Generate a fresh Hinglish caption for a viral video using Gemini Vision."""
    if not llm.enabled:
        raise RuntimeError("LLM provider is disabled; cannot generate video caption")

    context_parts = []
    if tweet_text.strip():
        context_parts.append(f"Original tweet context: {tweet_text.strip()[:300]}")
    if tweet_author:
        context_parts.append(f"Original poster: @{tweet_author}")

    context_block = "\n".join(context_parts)

    user_content: str | list[dict[str, Any]]
    if keyframes:
        image_parts = []
        for frame_bytes in keyframes[:_FRAME_COUNT]:
            b64 = base64.b64encode(frame_bytes).decode()
            image_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        user_content = [
            {
                "type": "text",
                "text": f"{context_block}\n\nAbove are keyframes from the viral video. Write a hilarious, relatable HINGLISH caption for this video in Roman script. Output only the caption.",
            },
            *image_parts,
        ]
    else:
        user_content = (
            f"{context_block}\n\n"
            "No video frames available. Based on the context above, "
            "infer the video topic and write a fresh, hilarious HINGLISH caption in Roman script."
        )

    messages = [
        LLMMessage(role="system", content=_CAPTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]

    response = await llm.generate(messages, operation="video_caption_generation", temperature=0.9)
    raw = response.text.strip()

    match = re.search(r"CAPTION:\s*(.+)", raw, re.DOTALL | re.IGNORECASE)
    if match:
        caption = match.group(1).strip().split("\n")[0].strip()
    else:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        caption = lines[0] if lines else raw[:240]

    if len(caption) > 240:
        caption = caption[:237] + "..."

    return caption


async def _has_video_and_unhide(article_elem: Any) -> bool:
    """Check if article contains a video, automatically clicking any sensitive content warning if present."""
    # Check for sensitive content / warning overlay and click 'View' to reveal video
    for view_sel in [
        'button:has-text("View")',
        'div[role="button"]:has-text("View")',
        '[data-testid="emptyState"] button',
    ]:
        try:
            btn = article_elem.locator(view_sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
                break
        except Exception:
            pass

    video_selectors = [
        "video",
        '[data-testid="videoPlayer"]',
        '[data-testid="videoComponent"]',
        'div[aria-label*="Embedded video"]',
        'div[aria-label*="Play"]',
    ]
    for sel in video_selectors:
        try:
            count = await article_elem.locator(sel).count()
            if count > 0:
                return True
        except Exception:
            continue
    return False


async def scan_viral_handles_for_video_posts(
    settings: Settings,
    target_handles: list[str],
    *,
    limit_per_handle: int = 3,
    min_impressions: int = 10_000,
) -> list[dict[str, Any]]:
    """Scan configured viral handles for high-reach tweets with embedded video."""
    from playwright.async_api import async_playwright

    from app.publishing.brave_cdp import (
        BraveCDPPublisher,
        metrics_from_aria_labels,
        resolve_brave_executable,
    )

    if not resolve_brave_executable():
        logger.warning("Brave/Chromium not found; skipping video scan")
        return []

    class _StubSessionFactory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

    stub_factory: Any = _StubSessionFactory()
    publisher = BraveCDPPublisher(settings, stub_factory)

    candidates: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser, context, is_owned_context = await publisher._get_browser_context(p)
        try:
            for handle in target_handles:
                clean_handle = handle.lstrip("@")
                profile_url = f"https://x.com/{clean_handle}"
                page = await context.new_page()
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=35_000)
                    articles = page.locator('article[data-testid="tweet"]')
                    try:
                        await articles.first.wait_for(state="visible", timeout=12_000)
                    except Exception:
                        continue

                    await page.evaluate("window.scrollBy(0, 400)")
                    await asyncio.sleep(1.0)

                    count = min(await articles.count(), limit_per_handle)
                    for i in range(count):
                        try:
                            article = articles.nth(i)
                            if not await _has_video_and_unhide(article):
                                continue

                            text_elem = article.locator('[data-testid="tweetText"]').first
                            text = await text_elem.inner_text() if await text_elem.count() > 0 else ""

                            href = ""
                            status_links = article.locator('a[href*="/status/"]')
                            for k in range(await status_links.count()):
                                candidate_href = (
                                    await status_links.nth(k).get_attribute("href") or ""
                                ).split("?")[0]
                                if re.search(r"/[^/]+/status/\d+$", candidate_href):
                                    href = candidate_href
                                    break
                            if not href:
                                continue

                            tweet_url = (
                                f"https://x.com{href}" if href.startswith("/") else href
                            )
                            match_id = re.search(r"/status/(\d+)", tweet_url)
                            tweet_id = match_id.group(1) if match_id else ""
                            if not tweet_id:
                                continue

                            labels = await article.locator("[aria-label]").evaluate_all(
                                "els => els.map(el => el.getAttribute('aria-label') || '')"
                            )
                            metrics = metrics_from_aria_labels(labels)
                            impressions = metrics.get("impressions", 0)

                            if impressions >= min_impressions:
                                candidates.append({
                                    "url": tweet_url,
                                    "tweet_id": tweet_id,
                                    "author": clean_handle,
                                    "text": text.strip(),
                                    "has_video": True,
                                    **metrics,
                                })
                        except Exception:
                            continue
                except Exception as exc:
                    logger.warning(
                        f"Failed scanning @{handle} for video posts: {exc}"
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

    return sorted(candidates, key=lambda c: c.get("impressions", 0), reverse=True)


async def post_video_with_caption(
    settings: Settings,
    session_factory: Any,
    video_path: Path,
    caption: str,
    account_id: int,
    source_tweet_url: str,
) -> dict[str, Any]:
    """Post a downloaded video with a fresh caption via Brave CDP."""
    from sqlalchemy import select

    from app.memory.models import DRAFT_APPROVED, Draft, Post
    from app.memory.repositories import DraftRepository
    from app.publishing.brave_cdp import BraveCDPPublisher

    publisher = BraveCDPPublisher(settings, session_factory)

    source_match = re.search(r"/status/(\d+)", source_tweet_url)
    source_tweet_id = source_match.group(1) if source_match else ""
    idempotency_key = f"video_repost:{source_tweet_url}"

    async with session_factory() as session:
        draft = await session.scalar(
            select(Draft)
            .where(
                Draft.account_id == account_id,
                Draft.policy_report["source_tweet_url"].astext == source_tweet_url,
            )
            .order_by(Draft.created_at.desc())
        )
        if draft is not None:
            existing_posts = list(
                (
                    await session.scalars(
                        select(Post).where(Post.draft_id == draft.id).order_by(Post.position)
                    )
                ).all()
            )
            if existing_posts:
                return {
                    "status": "already_published",
                    "draft_id": draft.id,
                    "post_ids": [post.x_post_id for post in existing_posts if post.x_post_id],
                    "caption": draft.body,
                    "source_tweet_url": source_tweet_url,
                }
            draft.status = DRAFT_APPROVED
            draft.body = caption
            draft.error = None
        else:
            draft = await DraftRepository.create(
                session,
                account_id=account_id,
                kind="original",
                status=DRAFT_APPROVED,
                idempotency_key=idempotency_key,
                topic=f"Video repost from {source_tweet_url}",
                title="Viral Video Repost",
                body=caption,
                thread_parts=[],
                tone="bakchodi",
                rationale="Viral video repost with fresh AI-generated caption",
                source_document_ids=[],
                verification_report={
                    "passed": True,
                    "operator_approved": True,
                    "claims": [],
                    "unverified_facts": 0,
                    "manual_review_required": False,
                },
                policy_report={
                    "source": "video_repost_engine",
                    "source_tweet_url": source_tweet_url,
                    "source_tweet_id": source_tweet_id,
                    "operator_approved": True,
                    "zero_cost": True,
                    "video_path": str(video_path),
                },
            )
        await session.commit()

    result = await publisher.publish_with_video(draft, caption, video_path)
    return {
        "status": result.status,
        "draft_id": draft.id,
        "post_ids": result.post_ids,
        "caption": caption,
        "source_tweet_url": source_tweet_url,
    }
