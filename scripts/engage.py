"""Outbound Engagement Runner for @ssh_bugs.

Allows the agent to act like a real person on X:
- Drops sarcastic or value-add comments on viral tweets.
- Quote-tweets breaking developments with our punchy perspective.
- Retweets high-velocity breaking updates.

Usage examples:
  # 1. Preview 3 banger takes for a specific viral tweet (dry-run):
  python -m scripts.engage --tweet https://x.com/ANI/status/1885544332211

  # 2. Publish a sarcastic reply live via Brave CDP:
  python -m scripts.engage --tweet https://x.com/ANI/status/1885544332211 --style sarcastic --live

  # 3. Quote-tweet a post with a value-add hook:
  python -m scripts.engage --tweet https://x.com/IndianTechGuide/status/1885544332211 --action quote --style value_add --live

  # 4. Retweet (repost) a post:
  python -m scripts.engage --tweet https://x.com/PTI_News/status/1885544332211 --action retweet --live

  # 5. Auto-scan latest tweet from top target accounts:
  python -m scripts.engage --auto --style sarcastic --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.growth.engagement import EngagementService, extract_tweet_metadata
from app.memory.database import dispose_engine, get_session_factory, init_engine
from app.memory.repositories import AccountRepository
from app.providers.factory import LLMService, build_provider
from app.publishing.brave_cdp import BraveCDPPublisher
from scripts._common import prepare


async def run_engagement(
    tweet_url: str | None,
    handle: str | None,
    auto: bool,
    action: str,
    style: str,
    live: bool,
    override_text: str | None = None,
) -> None:
    settings = prepare()
    init_engine(settings)

    try:
        provider = build_provider(settings)
        llm = LLMService(settings, provider, get_session_factory())
        publisher = BraveCDPPublisher(settings, get_session_factory())
        service = EngagementService(settings, llm, get_session_factory(), publisher=publisher)

        async with get_session_factory()() as session:
            account = await AccountRepository.get_default(session, settings.x_persona_handle)
            if not account:
                print(
                    "Error: Account not found in database. Run python -m scripts.bootstrap first."
                )
                return

        target_url = tweet_url or ""
        target_text = override_text or ""
        target_author = ""

        # Auto-scan if requested
        if auto or (not target_url and handle):
            target_handles = [handle] if handle else settings.growth_follow_handles
            if not target_handles:
                target_handles = ["IndianTechGuide", "NewsAlgebraIND", "ANI", "livemint"]

            print(f"[*] Scanning latest tweets from target handles: {', '.join(target_handles)}...")
            candidates = await service.scan_handles_for_candidates(
                target_handles, limit_per_handle=2
            )
            if not candidates:
                print("[-] No candidate tweets found from scanned profiles.")
                return

            chosen = candidates[0]
            target_url = chosen["url"]
            target_text = chosen["text"]
            target_author = chosen["author"]
            print(f"[+] Found candidate from @{target_author}: {target_url}")
            print(f"    Text: {target_text[:120]}...\n")

        # Retweet action shortcut
        if action == "retweet":
            if not target_url:
                print("Error: --tweet URL is required for retweet action.")
                return
            print(f"[*] Dispatching Retweet/Repost on {target_url} (live={live})...")
            if not live:
                print("[DRY RUN] Would execute retweet via Brave CDP.")
                return
            res = await service.repost(target_url)
            print(json.dumps(res, indent=2))
            return

        # Fetch tweet details if URL is provided but text is empty
        if target_url and not target_text:
            print(f"[*] Fetching tweet details for {target_url}...")
            details = await publisher.fetch_tweet_details(target_url)
            target_text = details.get("text", "")
            target_author = details.get("author", "")
            if not target_text:
                author_from_url, post_id = extract_tweet_metadata(target_url)
                target_author = author_from_url
                target_text = override_text or f"Trending discussion by @{author_from_url}"

        if not target_text:
            print("Error: Unable to resolve tweet content to engage with. Pass --text '<content>'")
            return

        print(f"[*] Analyzing target tweet from @{target_author or 'user'}:")
        print(f'    "{target_text}"\n')
        print("[*] Generating high-impact Indian commentary options with Gemini...")

        bangers = await service.generate_bangers(target_text, target_author=target_author)

        print("\n" + "=" * 60)
        print("⚡ GENERATED BANGER OPTIONS FOR @ssh_bugs")
        print("=" * 60)
        print(f"1. [SARCASTIC]  : {bangers.get('sarcastic')}")
        print(f"2. [VALUE-ADD]  : {bangers.get('value_add')}")
        print(f"3. [QUOTE-TWEET]: {bangers.get('quote_tweet')}")
        print("=" * 60 + "\n")

        chosen_content = bangers.get(style, bangers.get("sarcastic", ""))
        if not chosen_content:
            chosen_content = bangers.get("sarcastic") or bangers.get("value_add", "")

        print(f"[*] Selected Style '{style}' ({action}):")
        print(f'    "{chosen_content}"\n')

        if not live:
            print("[DRY RUN] Re-run with --live to dispatch directly to X via Brave CDP.")
            return

        print(f"[*] Creating {action} draft and dispatching via Brave CDP...")
        draft = await service.create_engagement_draft(
            account_id=account.id,
            target_post_url=target_url,
            content=chosen_content,
            action="quote" if action == "quote" else "reply",
            style=style,
        )

        result = await service.publish_engagement(draft.id)
        print(f"[+] Status: {result.status}")
        if result.post_ids:
            print(f"[+] Post IDs: {result.post_ids}")
        if result.reasons:
            print(f"[-] Details: {result.reasons}")

    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tweet", help="Target tweet URL (e.g. https://x.com/ANI/status/123456)")
    parser.add_argument(
        "--handle", help="Scan a specific target handle (e.g. ANI or IndianTechGuide)"
    )
    parser.add_argument(
        "--auto", action="store_true", help="Auto-scan target accounts from GROWTH_FOLLOW_TARGETS"
    )
    parser.add_argument(
        "--action",
        choices=["reply", "quote", "retweet"],
        default="reply",
        help="Engagement action type (reply, quote, or retweet)",
    )
    parser.add_argument(
        "--style",
        choices=["sarcastic", "value_add", "quote_tweet"],
        default="sarcastic",
        help="Commentary style",
    )
    parser.add_argument("--text", help="Manual target text override")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Publish live via Brave CDP (default is dry-run preview)",
    )

    args = parser.parse_args()

    if not args.tweet and not args.handle and not args.auto:
        parser.print_help()
        sys.exit(1)

    asyncio.run(
        run_engagement(
            tweet_url=args.tweet,
            handle=args.handle,
            auto=args.auto,
            action=args.action,
            style=args.style,
            live=args.live,
            override_text=args.text,
        )
    )


if __name__ == "__main__":
    main()
