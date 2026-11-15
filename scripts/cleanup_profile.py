"""Profile Cleanup Utility for @ssh_bugs.

Deletes old, dry, low-view AI news tweets from the user's X timeline
via headless Brave CDP browser automation, while preserving fresh bangers
and tweets with >50 views.

Usage:
  # Dry-run (preview which tweets would be deleted):
  python -m scripts.cleanup_profile

  # Live deletion on X:
  python -m scripts.cleanup_profile --live
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import delete

from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.models import Post
from app.publishing.brave_cdp import resolve_brave_executable
from scripts._common import prepare

# Keep-list keywords for our fresh, high-performing bangers
PROTECTED_KEYWORDS = [
    "hyderabad it folks",
    "38 people",
    "57 ai agents",
    "whatsapp chatbot",
    "stuck on the orr",
    "bangalore-rejection",
]


def parse_view_count(view_text: str) -> int:
    """Parse view string like '1', '45', '1.2K', '340K', '1.5M' into an integer."""
    clean = view_text.strip().replace(",", "")
    if not clean:
        return 0
    try:
        if "m" in clean.lower():
            num = float(re.sub(r"[^\d.]", "", clean))
            return int(num * 1_000_000)
        if "k" in clean.lower():
            num = float(re.sub(r"[^\d.]", "", clean))
            return int(num * 1_000)
        return int(re.sub(r"[^\d]", "", clean))
    except Exception:
        return 0


async def cleanup_timeline(handle: str, min_views: int = 50, live: bool = False) -> None:
    settings = prepare()
    init_engine(settings)

    clean_handle = handle.strip().lstrip("@")
    profile_url = f"https://x.com/{clean_handle}"

    print(f"[*] Starting Profile Cleanup for @{clean_handle} (live={live})...")
    print(f"[*] Policy: Delete tweets with <{min_views} views (except fresh protected bangers).\n")

    profile_dir = str(
        Path(getattr(settings, "brave_profile_dir", ".brave_profile")).resolve()  # noqa: ASYNC240
    )
    brave_exe = resolve_brave_executable()

    if not brave_exe:
        print("[-] Error: Brave/Chromium executable not found.")
        return

    async with async_playwright() as p:
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
            user_data_dir=profile_dir,
            executable_path=brave_exe,
            headless=is_headless,
            args=launch_args,
        )

        # Inject session cookies if available
        auth_token = (
            settings.x_auth_token.get_secret_value() if settings.x_auth_token else None
        )
        ct0 = settings.x_ct0.get_secret_value() if settings.x_ct0 else None

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

        try:
            page = await context.new_page()
            print(f"[*] Navigating to {profile_url}...")
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=40000)
            except Exception as nav_err:
                print(f"[*] Navigation note ({nav_err}), navigating via home...")
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2.0)
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4.0)

            # Wait for articles to load
            articles = page.locator('article[data-testid="tweet"]')
            try:
                await articles.first.wait_for(state="visible", timeout=12000)
            except Exception:
                print("[-] Could not find any tweets on the profile timeline.")
                return

            deleted_count = 0
            kept_count = 0
            max_deletions = 200
            attempted_texts: set[str] = set()

            if not live:
                # Dry run preview across 6 scrolls
                for scroll_idx in range(6):
                    articles = page.locator('article[data-testid="tweet"]')
                    count = await articles.count()
                    print(f"[*] Pass {scroll_idx + 1}: Found {count} tweets on screen...")

                    for i in range(count):
                        article = articles.nth(i)
                        text_elem = article.locator('[data-testid="tweetText"]').first
                        tweet_text = (
                            await text_elem.inner_text() if await text_elem.count() > 0 else ""
                        )
                        lower_text = tweet_text.lower()

                        if any(kw in lower_text for kw in PROTECTED_KEYWORDS):
                            print(f"[KEEP (Protected Banger)] \"{tweet_text[:60]}...\"")
                            kept_count += 1
                            continue

                        analytics_link = article.locator('a[href*="/analytics"]').first
                        view_count = 0
                        if await analytics_link.count() > 0:
                            view_count = parse_view_count(await analytics_link.inner_text())

                        if view_count >= min_views:
                            print(f"[KEEP (Views >= {min_views})] \"{tweet_text[:60]}...\" (Views: {view_count})")
                            kept_count += 1
                            continue

                        print(f"[DELETE CANDIDATE] \"{tweet_text[:60]}...\" (Views: {view_count})")
                        print("   [DRY RUN] Would click caret -> Delete -> Confirm.")
                        deleted_count += 1

                    await page.evaluate("window.scrollBy(0, 1000)")
                    await asyncio.sleep(2.0)

            else:
                # Live Deletion loop: continuously delete until done or bottom reached
                print("[*] Starting continuous live deletion loop...")
                consecutive_idle_passes = 0
                while deleted_count < max_deletions and consecutive_idle_passes < 4:
                    articles = page.locator('article[data-testid="tweet"]')
                    count = await articles.count()
                    if count == 0:
                        break

                    candidate_deleted = False
                    for i in range(count):
                        try:
                            article = articles.nth(i)
                            if not await article.is_visible():
                                continue

                            text_elem = article.locator('[data-testid="tweetText"]').first
                            tweet_text = (
                                await text_elem.inner_text() if await text_elem.count() > 0 else ""
                            )
                            lower_text = tweet_text.lower()

                            # Check if protected
                            if any(kw in lower_text for kw in PROTECTED_KEYWORDS):
                                continue

                            # Check if already attempted and failed
                            if tweet_text[:50] in attempted_texts:
                                continue

                            # Check view count
                            view_count = 0
                            analytics_link = article.locator('a[href*="/analytics"]').first
                            if await analytics_link.count() > 0:
                                view_count = parse_view_count(await analytics_link.inner_text())

                            if view_count >= min_views:
                                continue

                            # Delete this candidate!
                            print(f"[*] Deleting candidate #{deleted_count + 1}: \"{tweet_text[:60]}...\" (Views: {view_count})")
                            caret = article.locator('[data-testid="caret"]').first
                            if await caret.count() == 0:
                                attempted_texts.add(tweet_text[:50])
                                continue

                            await caret.scroll_into_view_if_needed()
                            await caret.click()
                            await asyncio.sleep(1.0)

                            delete_btn = page.locator('[role="menuitem"]:has-text("Delete")').first
                            if await delete_btn.count() == 0:
                                delete_btn = page.locator('span:text-is("Delete")').first

                            if await delete_btn.count() > 0:
                                await delete_btn.click()
                                await asyncio.sleep(1.0)

                                confirm_btn = page.locator('[data-testid="confirmationSheetConfirm"]').first
                                await confirm_btn.wait_for(state="visible", timeout=6000)
                                await confirm_btn.click()
                                await asyncio.sleep(2.0)

                                print(f"   [+] DELETED live from X: \"{tweet_text[:60]}...\"")
                                deleted_count += 1
                                candidate_deleted = True
                                consecutive_idle_passes = 0
                                # Break out so we re-read updated articles from DOM
                                break
                            else:
                                await page.keyboard.press("Escape")
                                await asyncio.sleep(0.5)
                                attempted_texts.add(tweet_text[:50])

                        except Exception as err:
                            print(f"   [-] Error deleting tweet: {err}")
                            with contextlib.suppress(Exception):
                                await page.keyboard.press("Escape")
                            attempted_texts.add(tweet_text[:50])
                            continue

                    if not candidate_deleted:
                        consecutive_idle_passes += 1
                        print(f"[*] No candidates in current viewport, scrolling down (idle pass {consecutive_idle_passes}/4)...")
                        await page.evaluate("window.scrollBy(0, 800)")
                        await asyncio.sleep(2.0)

            print("\n" + "=" * 50)
            print(f"CLEANUP SUMMARY: Kept: {kept_count} | Deleted: {deleted_count}")
            print("=" * 50)

        finally:
            with contextlib.suppress(Exception):
                await context.close()

    # Also clean local database post records if live
    if live and deleted_count > 0:
        try:
            async with get_session_factory()() as session:
                # Remove old boring news posts
                for kw in ["emergency", "pune", "landing gear", "precautionary"]:
                    await session.execute(
                        delete(Post).where(Post.text.ilike(f"%{kw}%"))
                    )
                await session.commit()
                print("[+] Cleaned matching dead posts from local database.")
        except Exception as e:
            print(f"[*] Database note: {e}")

    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", default="ssh_bugs", help="X profile handle to clean")
    parser.add_argument(
        "--min-views", type=int, default=50, help="Minimum views to preserve (default: 50)"
    )
    parser.add_argument(
        "--live", action="store_true", help="Execute deletion live on X (default is dry-run preview)"
    )
    args = parser.parse_args()

    asyncio.run(cleanup_timeline(handle=args.handle, min_views=args.min_views, live=args.live))


if __name__ == "__main__":
    main()
