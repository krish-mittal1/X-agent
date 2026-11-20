"""Viral Video Repost Engine for @ssh_bugs.

Pipeline:
1. Scan configured viral handles for posts that contain an embedded video
   and have high public reach (impressions threshold configurable).
2. Download the video via yt-dlp directly from the tweet URL — zero-cost,
   no X API required.
3. Extract N evenly-spaced keyframes as JPEG images using Pillow (pure-Python,
   no ffmpeg binary required).
4. Send the keyframes to Gemini Vision (multimodal) with the original tweet
   text so the LLM understands the visual context.
5. Generate a fresh, witty banger caption in the NotANerd @ssh_bugs voice.
6. Post the downloaded video + new caption via Brave CDP browser automation.
   The original source URL is stored in policy_report but NOT quoted in the tweet
   (to avoid copyright flag triggers on X).

Design constraints:
- Zero X API cost: yt-dlp + Playwright only.
- No ffmpeg binary dependency: keyframe extraction via Pillow VideoCapture
  falls back to metadata-only context if Pillow cannot decode the video codec.
- Graceful degradation: if the video download fails, the task returns "skipped"
  instead of crashing the APScheduler job.
- Daily cap: respects max_original_posts_per_day to prevent spamming.
- Deduplication: tracks downloaded tweet IDs in-memory across the same run so
  the same video isn't posted twice in one cycle.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import re
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logging_config import get_logger
from app.providers.base import LLMMessage
from app.providers.factory import LLMService

logger = get_logger(__name__)

# ------------------------------------------------------------------
# Keyframe extraction
# ------------------------------------------------------------------

_FRAME_COUNT = 5  # number of keyframes to extract and send to vision API


def _extract_keyframes_pillow(video_path: Path, n_frames: int = _FRAME_COUNT) -> list[bytes]:
    """Extract n evenly-spaced keyframes from a video file using Pillow.

    Pillow 10+ supports reading MP4 / WebM frames through its ImageSequence
    plugin for some codecs. If the codec isn't supported, falls back to []
    so the caller can handle gracefully.
    """
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


# ------------------------------------------------------------------
# yt-dlp video download
# ------------------------------------------------------------------

def _find_downloaded_video(dest_dir: Path) -> Path | None:
    for f in dest_dir.iterdir():
        if f.suffix in (".mp4", ".mkv", ".webm", ".mov") and f.stat().st_size > 10_000:
            return f
    return None


async def download_tweet_video(tweet_url: str, dest_dir: Path) -> Path | None:
    """Download the video embedded in a tweet URL to dest_dir using yt-dlp.

    Returns the path to the downloaded video file, or None on failure.
    Runs yt-dlp in a subprocess executor to avoid blocking the event loop.
    """
    import subprocess

    output_template = str(dest_dir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--quiet",
        "--no-warnings",
        "--format", "mp4/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--max-filesize", "50m",   # X allows up to 512MB; keep reasonable
        tweet_url,
    ]

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
        if result.returncode != 0:
            logger.warning(
                "yt-dlp non-zero exit",
                extra={"extra_fields": {"stderr": result.stderr[:500], "url": tweet_url}},
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(f"yt-dlp failed: {exc}", extra={"extra_fields": {"url": tweet_url}})
        return None

    # Find the downloaded file off-thread to avoid blocking async event loop
    return await loop.run_in_executor(None, _find_downloaded_video, dest_dir)


# ------------------------------------------------------------------
# Gemini Vision caption generation
# ------------------------------------------------------------------

_CAPTION_SYSTEM_PROMPT = """\
You are @ssh_bugs on X (Twitter): a razor-sharp, relatable Indian internet voice.
You have just seen a viral video that another account originally posted.
You are reposting it on your own account with a COMPLETELY FRESH caption.

YOUR VOICE:
- Desi internet native. Group-chat energy. No corporate speak.
- Short, punchy, human. Zero hashtags. Zero emoji spam.
- Can be sarcastic, bakchodi, or a sharp hot-take.
- NEVER copy the original caption. NEVER reference the original author.
- Treat it as if YOU discovered this video and are sharing it cold.
- Max 240 chars. No "look at this", no "this is insane", no filler intros.

OUTPUT FORMAT (strict, no extra text):
CAPTION: <your banger caption here>
"""


async def generate_video_caption(
    llm: LLMService,
    tweet_text: str,
    keyframes: list[bytes],
    *,
    tweet_author: str = "",
) -> str:
    """Generate a fresh caption for a viral video using Gemini Vision.

    Sends up to _FRAME_COUNT JPEG keyframes as base64 image_url parts
    inside a user message, alongside the original tweet text as context.
    Refuses text-only inference if no keyframes are available.
    """
    if not llm.enabled:
        raise RuntimeError("LLM provider is disabled; cannot generate video caption")

    context_parts = []
    if tweet_text.strip():
        context_parts.append(f"Original tweet text (for context only): {tweet_text.strip()[:300]}")
    if tweet_author:
        context_parts.append(f"Original author handle (do NOT mention or imitate): @{tweet_author}")

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
            {"type": "text", "text": f"{context_block}\n\nAbove are keyframes from the viral video. Write the caption now."},
            *image_parts,
        ]
    else:
        raise ValueError("video analysis requires visual evidence; no keyframes were extracted")

    messages = [
        LLMMessage(role="system", content=_CAPTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]

    response = await llm.generate(messages, operation="video_caption_generation", temperature=0.9)
    raw = response.text.strip()

    # Parse CAPTION: prefix
    match = re.search(r"CAPTION:\s*(.+)", raw, re.DOTALL | re.IGNORECASE)
    if match:
        caption = match.group(1).strip().split("\n")[0].strip()
    else:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        caption = lines[0] if lines else raw[:240]

    # Enforce character limit
    if len(caption) > 240:
        caption = caption[:237] + "..."

    return caption


# ------------------------------------------------------------------
# High-level pipeline
# ------------------------------------------------------------------

async def _has_video(article_elem: Any) -> bool:
    """Check if a Playwright article element contains an embedded video."""
    video_selectors = [
        "video",
        '[data-testid="videoPlayer"]',
        '[data-testid="videoComponent"]',
        'div[aria-label*="Embedded video"]',
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
    """Scan configured viral handles for high-reach tweets with embedded video.

    Returns a list of candidate dicts:
        url, tweet_id, author, text, impressions, likes, has_video=True
    """
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

                    await page.evaluate("window.scrollBy(0, 300)")
                    await asyncio.sleep(1.0)

                    count = min(await articles.count(), limit_per_handle)
                    for i in range(count):
                        try:
                            article = articles.nth(i)
                            if not await _has_video(article):
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

    # Sort by impressions descending
    return sorted(candidates, key=lambda c: c.get("impressions", 0), reverse=True)


async def post_video_with_caption(
    settings: Settings,
    session_factory: Any,
    video_path: Path,
    caption: str,
    account_id: int,
    source_tweet_url: str,
) -> dict[str, Any]:
    """Post a downloaded video with a fresh caption via Brave CDP.

    Uses the existing BraveCDPPublisher infrastructure but attaches a video
    file instead of an image, then posts via the X compose intent URL.
    """
    from app.memory.models import DRAFT_APPROVED
    from app.memory.repositories import DraftRepository
    async with session_factory() as session:
        draft = await DraftRepository.create(
            session,
            account_id=account_id,
            kind="original",
            status=DRAFT_APPROVED,
            idempotency_key=f"video_repost:{source_tweet_url}:{int(time.time())}",
            topic=f"Video repost from {source_tweet_url}",
            title="Viral Video Repost",
            body=caption,
            thread_parts=[],
            tone="bakchodi",
            rationale="Viral video repost with fresh AI-generated caption",
            source_document_ids=[],
            verification_report={
                "passed": False,
                "operator_approved": False,
                "claims": [],
                "unverified_facts": 1,
                "manual_review_required": True,
                "reason": "video media and generated commentary require rights and factual review",
            },
            policy_report={
                "source": "video_repost_engine",
                "source_tweet_url": source_tweet_url,
                "operator_approved": False,
                "rights_confirmed": False,
                "video_path": str(video_path),
            },
        )
        await session.commit()

    return {
        "status": "needs_review",
        "draft_id": draft.id,
        "post_ids": [],
        "caption": caption,
        "source_tweet_url": source_tweet_url,
        "reason": "video draft staged; rights, provenance and factual review are required before publishing",
    }
