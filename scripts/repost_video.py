"""Viral Video Repost Tool for @ssh_bugs.

Scans viral Indian accounts (or takes a specific tweet URL), downloads the video,
bypasses sensitive content & safety warnings, extracts keyframes with Pillow,
generates a fresh banger caption with Gemini Vision, and posts via Brave CDP.

Usage:
    # 1. Repost from a specific tweet URL (dry run):
    python -m scripts.repost_video --tweet https://x.com/gharkekalesh/status/1234567890

    # 2. Repost from a specific tweet URL (live on @ssh_bugs):
    python -m scripts.repost_video --tweet https://x.com/gharkekalesh/status/1234567890 --live

    # 3. Auto-scan viral handles and post top viral video (live):
    python -m scripts.repost_video --auto --live
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from app.growth.video_repost import (
    _extract_keyframes,
    download_tweet_video,
    generate_video_caption,
    post_video_with_caption,
    scan_viral_handles_for_video_posts,
)
from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.repositories import AccountRepository
from app.providers.factory import LLMService, build_provider
from scripts._common import prepare


async def main_async(args: argparse.Namespace) -> None:
    settings = prepare()
    if args.live:
        if not settings.publishing_enabled or settings.dry_run:
            raise SystemExit(
                "Live video posting requires PUBLISHING_ENABLED=true and DRY_RUN=false"
            )
        if settings.emergency_pause:
            raise SystemExit("Live video posting blocked: EMERGENCY_PAUSE=true")
        if not settings.viral_video_rights_confirmed:
            raise SystemExit(
                "Live video posting requires confirmed ownership or reuse rights "
                "(VIRAL_VIDEO_RIGHTS_CONFIRMED=true)"
            )

    session_factory = None
    account_id = 1
    try:
        init_engine(settings)
        session_factory = get_session_factory()
        async with session_factory() as session:
            account = await AccountRepository.get_default(session, settings.x_persona_handle)
            if not account:
                account = await AccountRepository.create(
                    session,
                    handle=settings.x_persona_handle,
                    display_name="NotANerd",
                    is_active=True,
                )
                await session.commit()
            account_id = account.id
    except Exception as db_err:
        if args.live:
            print(f"[-] Database error during live run: {db_err}")
            raise
        print(f"[*] Note: Local PostgreSQL not connected ({db_err}). Running in standalone preview mode.")
        from unittest.mock import AsyncMock, MagicMock
        fake_session = AsyncMock()
        session_factory = MagicMock(return_value=fake_session)

    try:
        provider = build_provider(settings)
        llm = LLMService(settings, provider, session_factory)

        target_url = args.tweet
        tweet_text = args.text or ""
        tweet_author = ""

        if not target_url and (args.auto or args.list):
            print("[*] Scanning viral handles for video posts with high reach...", flush=True)
            if args.handles:
                target_handles = [h.strip() for h in args.handles.split(",") if h.strip()]
            else:
                configured = [h.strip() for h in getattr(settings, "viral_target_handles", "").split(",") if h.strip()]
                target_handles = configured or [
                    "gharkekalesh", "DeadlyKalesh", "KaleshChahiye", "crazyclips_",
                    "sagarcasm", "GabbbarSingh", "coolfunnytshirt", "Trend_ulkar",
                    "memenist", "desimojito", "thetatva", "IndianTechGuide", "peakbengaluru"
                ]

            print(f"[*] Scanning {len(target_handles)} handles: {', '.join(target_handles[:5])}...", flush=True)
            candidates = await scan_viral_handles_for_video_posts(
                settings,
                target_handles,
                limit_per_handle=args.limit,
                min_impressions=(
                    args.min_impressions
                    if args.min_impressions is not None
                    else settings.outbound_min_impressions
                ),
            )
            if not candidates:
                print("[-] No high-reach video posts found across viral handles.", flush=True)
                return

            print(f"\n[+] Found {len(candidates)} high-impression video candidates:", flush=True)
            for idx, c in enumerate(candidates, 1):
                views = c.get("impressions", 0)
                author = c.get("author", "unknown")
                text = (c.get("text") or "")[:80].replace("\n", " ")
                print(f"  {idx}. [{views:,} views] @{author}: \"{text}\"", flush=True)
                print(f"     URL: {c['url']}", flush=True)

            if args.list:
                return

            chosen_idx = max(0, min(args.pick - 1, len(candidates) - 1)) if args.pick else 0
            top = candidates[chosen_idx]
            target_url = top["url"]
            tweet_text = top.get("text", "")
            tweet_author = top.get("author", "")
            print(f"\n[*] Selected candidate #{chosen_idx + 1}: {target_url} (Views: {top.get('impressions', 0):,})", flush=True)

        if not target_url:
            print("[-] Error: Please specify --tweet <url> or use --auto.", flush=True)
            return

        auth_token = (
            settings.x_auth_token.get_secret_value()
            if settings.x_auth_token
            else None
        )
        ct0 = (
            settings.x_ct0.get_secret_value()
            if settings.x_ct0
            else None
        )

        with tempfile.TemporaryDirectory(prefix="repost_video_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            print(f"[*] Downloading video from {target_url} (bypassing sensitive/safety gates)...", flush=True)

            video_file = await download_tweet_video(
                target_url,
                tmp_path,
                auth_token=auth_token,
                ct0=ct0,
                settings=settings,
            )

            if not video_file or not video_file.exists():
                print("[-] Failed: Could not download video from tweet.", flush=True)
                return

            size_mb = video_file.stat().st_size / (1024 * 1024)
            print(f"[+] Video downloaded successfully: {video_file.name} ({size_mb:.2f} MB)", flush=True)

            print("[*] Extracting keyframes for Gemini Vision...", flush=True)
            keyframes: list[bytes] = []
            with contextlib.suppress(Exception):
                keyframes = _extract_keyframes(video_file)

            print(f"[+] Extracted {len(keyframes)} keyframes.", flush=True)

            if args.caption:
                caption = args.caption
                print(f"\n[+] Using operator-provided Hinglish Caption:\n>>> {caption}\n", flush=True)
            else:
                print("[*] Generating fresh NotANerd (@ssh_bugs) Hinglish caption with Gemini Vision...", flush=True)
                caption = await generate_video_caption(
                    llm,
                    tweet_text,
                    keyframes,
                    tweet_author=tweet_author,
                )
                print(f"\n[+] Generated Hinglish Caption:\n>>> {caption}\n", flush=True)

            if not args.live:
                print("[!] DRY RUN ONLY: Video was downloaded and Hinglish caption was generated.", flush=True)
                print("[!] To post live to @ssh_bugs on X, run with --live flag.", flush=True)
                return

            print("[*] Posting video + Hinglish caption live via Brave CDP...", flush=True)
            result = await post_video_with_caption(
                settings=settings,
                session_factory=session_factory,
                video_path=video_file,
                caption=caption,
                account_id=account_id,
                source_tweet_url=target_url,
            )

            print(f"\n[+] Publishing Result:\n{json.dumps(result, indent=2)}", flush=True)

    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download viral X video and repost with Hinglish caption.")
    parser.add_argument("--tweet", help="Specific tweet URL containing video to repost.")
    parser.add_argument("--auto", action="store_true", help="Auto-scan viral handles for top video.")
    parser.add_argument("--list", action="store_true", help="Find and list high-impression video candidates without posting.")
    parser.add_argument("--handles", help="Comma-separated viral handles to scan (e.g. gharkekalesh,crazyclips_).")
    parser.add_argument("--pick", type=int, default=1, help="Pick candidate index (1-based) from scan results.")
    parser.add_argument("--caption", help="Override with custom Hinglish caption instead of AI generation.")
    parser.add_argument("--text", help="Optional original tweet text context if known.")
    parser.add_argument("--limit", type=int, default=3, help="Max posts to inspect per handle.")
    parser.add_argument(
        "--min-impressions",
        type=int,
        default=None,
        help="Min impressions filter (defaults to OUTBOUND_MIN_IMPRESSIONS).",
    )
    parser.add_argument("--live", action="store_true", help="Actually post live to @ssh_bugs.")

    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
