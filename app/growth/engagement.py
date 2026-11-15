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

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_post_url": self.target_post_url,
            "target_post_id": self.target_post_id,
            "target_author": self.target_author,
            "target_text": self.target_text,
            "sarcastic": self.sarcastic,
            "value_add": self.value_add,
            "quote_tweet": self.quote_tweet,
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
        """Generate 3 viral Indian engagement takes: sarcastic, value-add, and quote-tweet."""
        clean_text = target_text.strip()
        if not clean_text:
            raise ValueError("target_text cannot be empty")

        system_prompt = (
            "You are @ssh_bugs on X, an ultra-sharp, observant, high-IQ Indian commentator.\n"
            "You are looking at a viral tweet and drafting 3 distinct, scroll-stopping responses.\n\n"
            "YOUR VOICE & IDENTITY:\n"
            "- You write like a real, witty human in an Indian group chat or tech/news circle.\n"
            "- Conversational, plain everyday English that hits in 2 seconds.\n"
            "- ZERO AI FLUFF: Never use 'delve', 'testament to', 'game changer', 'it is worth noting', 'in my opinion'.\n"
            "- ZERO HASHTAGS: Never include hashtags or marketing buzzwords.\n\n"
            "THE 3 REQUIRED STYLES:\n"
            "1. SARCASTIC (Max 180 chars): Witty, dry reality-check cutting through the PR, spin, or hypocrisy. Highlight the unspoken irony.\n"
            "2. VALUE_ADD (Max 220 chars): The missing context, real numbers, or direct impact on an everyday Indian's wallet or life.\n"
            "3. QUOTE_TWEET (Max 220 chars): An above-the-fold hook for quote-tweeting this on our timeline to start a debate.\n\n"
            "OUTPUT FORMAT (Strict):\n"
            "SARCASTIC: <text under 180 chars>\n"
            "VALUE_ADD: <text under 220 chars>\n"
            "QUOTE_TWEET: <text under 220 chars>"
        )

        user_content = (
            f'TARGET TWEET:\n"""{clean_text}"""\n'
            + (f"TARGET AUTHOR: @{target_author}\n" if target_author else "")
            + (f"ADDITIONAL CONTEXT: {context}\n" if context else "")
            + "Draft the 3 distinct bangers now:"
        )

        resp = await self.llm.generate(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_content),
            ],
            operation="generate_bangers",
            temperature=0.75,
            max_tokens=300,
        )

        raw = resp.text.strip()
        parsed: dict[str, str] = {
            "sarcastic": "",
            "value_add": "",
            "quote_tweet": "",
        }

        for line in raw.splitlines():
            line_str = line.strip()
            if line_str.upper().startswith("SARCASTIC:"):
                parsed["sarcastic"] = line_str[10:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("VALUE_ADD:"):
                parsed["value_add"] = line_str[10:].strip().strip('"').strip("'")
            elif line_str.upper().startswith("QUOTE_TWEET:"):
                parsed["quote_tweet"] = line_str[12:].strip().strip('"').strip("'")

        # Fallback if raw text lacked keys
        if not parsed["sarcastic"]:
            valid_lines = [
                line.strip().strip('"') for line in raw.splitlines() if len(line.strip()) > 10
            ]
            if valid_lines:
                parsed["sarcastic"] = valid_lines[0]
            if len(valid_lines) > 1:
                parsed["value_add"] = valid_lines[1]
            if len(valid_lines) > 2:
                parsed["quote_tweet"] = valid_lines[2]

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
            return draft

    async def publish_engagement(self, draft_id: int) -> PublishResult:
        """Publish an approved reply or quote draft directly via Brave CDP."""
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            if not draft:
                raise ValueError(f"Draft {draft_id} not found")

        parts = [draft.body]
        return await self.publisher.publish(draft, parts)

    async def repost(self, target_post_url: str) -> dict[str, Any]:
        """Directly retweet/repost a target tweet via Brave browser."""
        return await self.publisher.repost(target_post_url)

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
