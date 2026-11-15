"""Outbound Engagement & Infiltration Engine for @ssh_bugs.

Allows the agent to act like a real person on X:
1. Scan high-velocity target accounts (e.g. ANI, IndianTechGuide, NewsAlgebraIND).
2. Generate 3 distinct banger response options:
   - Sarcastic: witty, ironic, sharp reality-check.
   - Value-Add: missing metrics, hidden catch, consumer/wallet impact.
   - Quote-Tweet: above-the-fold hook for an independent take on our feed.
3. Publish via Brave CDP browser automation (reply, quote-tweet, or retweet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.memory.models import DRAFT_READY, Draft
from app.memory.repositories import DraftRepository
from app.providers.base import LLMMessage
from app.providers.factory import LLMService
from app.publishing.brave_cdp import BraveCDPPublisher
from app.schemas.publishing import PublishResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class BangerProposal:
    target_post_url: str
    target_post_id: str
    target_author: str
    target_text: str
    sarcastic: str
    value_add: str
    quote_tweet: str
    rage_bait: str = ""
    bakchodi: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_post_url": self.target_post_url,
            "target_post_id": self.target_post_id,
            "target_author": self.target_author,
            "target_text": self.target_text,
            "sarcastic": self.sarcastic,
            "value_add": self.value_add,
            "quote_tweet": self.quote_tweet,
            "rage_bait": self.rage_bait,
            "bakchodi": self.bakchodi,
        }


def extract_tweet_metadata(tweet_url: str) -> tuple[str, str]:
    """Extract (author_handle, tweet_id) from an X/Twitter status URL."""
    clean = tweet_url.strip()
    match = re.search(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)", clean)
    if match:
        return match.group(1), match.group(2)
    return "", ""


class EngagementService:
    def __init__(
        self,
        settings: Settings,
        llm: LLMService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        publisher: BraveCDPPublisher | None = None,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.session_factory = session_factory
        self.publisher = publisher or BraveCDPPublisher(settings, session_factory, clock=clock)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def generate_bangers(
        self,
        target_text: str,
        *,
        target_author: str = "",
        context: str = "",
    ) -> dict[str, str]:
        """Generate all-rounder viral takes: sarcastic, rage_bait, bakchodi, quote_tweet, value_add."""
        clean_text = target_text.strip()
        if not clean_text:
            raise ValueError("target_text cannot be empty")

        system_prompt = (
            "You are @ssh_bugs on X, an all-rounder Indian internet voice (mix of @sagarcasm, @GabbbarSingh, and @peakbengaluru).\n"
            "You are looking at a viral tweet and drafting distinct, scroll-stopping responses for maximum reach and debate.\n\n"
            "YOUR VOICE & IDENTITY:\n"
            "- Ultra-relatable, sharp, conversational, desi internet native.\n"
            "- You do bakchodi, sarcastic burns, rage-baiting takes that provoke arguments, and funny 1-line quote captions.\n"
            "- ZERO AI FLUFF: Never say 'delve', 'testament to', 'game changer', 'it remains to be seen', 'in my opinion'.\n"
            "- ZERO HASHTAGS: Never include hashtags or robotic emojis.\n\n"
            "THE 5 REQUIRED ALL-ROUNDER STYLES:\n"
            "1. SARCASTIC (Max 180 chars): Witty, dry reality-check cutting through the PR or hypocrisy with conversational irony.\n"
            "2. RAGE_BAIT (Max 200 chars): A spicy, controversial unpopular opinion that forces people to comment and argue.\n"
            "3. BAKCHODI (Max 180 chars): Pure desi group-chat humor, relatable adulting/office/life observation, deadpan punchline.\n"
            "4. QUOTE_TWEET (Max 180 chars): A killer 1-line headline caption to place on top of the quoted tweet (like 'Bro thought he was...').\n"
            "5. VALUE_ADD (Max 220 chars): The unspoken financial reality, hidden cost, or hard metrics.\n\n"
            "OUTPUT FORMAT (Strict):\n"
            "SARCASTIC: <text under 180 chars>\n"
            "RAGE_BAIT: <text under 200 chars>\n"
            "BAKCHODI: <text under 180 chars>\n"
            "QUOTE_TWEET: <text under 180 chars>\n"
            "VALUE_ADD: <text under 220 chars>"
        )

        user_content = (
            f'TARGET TWEET:\n"""{clean_text}"""\n'
            + (f"TARGET AUTHOR: @{target_author}\n" if target_author else "")
            + (f"ADDITIONAL CONTEXT: {context}\n" if context else "")
            + "Draft the all-rounder bangers now:"
        )

        resp = await self.llm.generate(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_content),
            ],
            operation="generate_bangers",
            temperature=0.82,
            max_tokens=400,
        )

        raw = resp.text.strip()
        parsed: dict[str, str] = {
            "sarcastic": "",
            "rage_bait": "",
            "bakchodi": "",
            "quote_tweet": "",
            "value_add": "",
        }

        for line in raw.splitlines():
            line_str = line.strip()
            if line_str.upper().startswith("SARCASTIC:"):
                parsed["sarcastic"] = line_str[10:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("RAGE_BAIT:"):
                parsed["rage_bait"] = line_str[10:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("BAKCHODI:"):
                parsed["bakchodi"] = line_str[9:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("QUOTE_TWEET:"):
                parsed["quote_tweet"] = line_str[12:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("VALUE_ADD:"):
                parsed["value_add"] = line_str[10:].strip().strip('"').strip("'")

        # Fallback if raw text lacked keys
        if not parsed["sarcastic"]:
            valid_lines = [
                line.strip().strip('"') for line in raw.splitlines() if len(line.strip()) > 10
            ]
            if valid_lines:
                parsed["sarcastic"] = valid_lines[0]
            if len(valid_lines) > 1:
                parsed["rage_bait"] = valid_lines[1]
            if len(valid_lines) > 2:
                parsed["quote_tweet"] = valid_lines[2]
            if len(valid_lines) > 3:
                parsed["bakchodi"] = valid_lines[3]
            if len(valid_lines) > 4:
                parsed["value_add"] = valid_lines[4]

        return parsed

    async def create_engagement_draft(
        self,
        account_id: int,
        target_post_url: str,
        content: str,
        *,
        action: Literal["reply", "quote"] = "reply",
        style: str = "sarcastic",
    ) -> Draft:
        """Create and persist a draft for a reply or quote-tweet."""
        author, post_id = extract_tweet_metadata(target_post_url)
        key = f"engagement:{action}:{account_id}:{post_id}:{int(self.clock().timestamp())}"

        policy_report: dict[str, Any] = {
            "action": action,
            "style": style,
            "target_post_url": target_post_url,
            "target_author": author,
            "target_post_id": post_id,
            "source": "operator_command",
            "zero_cost": True,
        }
        if action == "quote":
            policy_report["quoted_post_url"] = target_post_url

        async with self.session_factory() as session:
            draft = await DraftRepository.create(
                session,
                account_id=account_id,
                kind="reply" if action == "reply" else "original",
                status=DRAFT_READY,
                idempotency_key=key,
                topic=f"Engagement on @{author}: {post_id}",
                title=f"Engagement on @{author}",
                body=content.strip(),
                thread_parts=[],
                tone=style,
                rationale=f"High-impact {action} engagement targeting @{author}",
                source_document_ids=[],
                in_reply_to_post_id=post_id if action == "reply" else None,
                target_author_handle=author if author else None,
                policy_report=policy_report,
            )
            await session.commit()
            return draft

    async def publish_engagement(self, draft_or_id: int | Draft) -> PublishResult:
        """Publish an approved reply or quote draft directly via Brave CDP."""
        target_draft: Draft
        if isinstance(draft_or_id, Draft):
            target_draft = draft_or_id
        else:
            async with self.session_factory() as session:
                loaded = await session.get(Draft, draft_or_id)
                if loaded is None:
                    raise ValueError(f"Draft {draft_or_id} not found")
                target_draft = loaded

        parts = [target_draft.body]
        return await self.publisher.publish(target_draft, parts)

    async def repost(self, target_post_url: str) -> dict[str, Any]:
        """Directly retweet/repost a target tweet via Brave browser."""
        return await self.publisher.repost(target_post_url)

    async def like(self, target_post_url: str) -> dict[str, Any]:
        """Like a target tweet via Brave browser (natural human engagement)."""
        return await self.publisher.like(target_post_url)

    async def scan_handles_for_candidates(
        self, handles: list[str], limit_per_handle: int = 2
    ) -> list[dict[str, Any]]:
        """Scan target accounts for their latest breaking tweets."""
        candidates: list[dict[str, Any]] = []
        for handle in handles:
            clean = handle.strip().lstrip("@")
            if not clean:
                continue
            tweets = await self.publisher.scan_profile_tweets(clean, limit=limit_per_handle)
            candidates.extend(tweets)
        return candidates
