"""Draft replies to inbound conversations. Drafts are never sent from here."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.persona import load_persona
from app.agent.prompts import build_reply_messages
from app.config import Settings
from app.logging_config import get_logger
from app.memory.embeddings import EmbeddingProvider
from app.memory.models import (
    DRAFT_BLOCKED,
    DRAFT_NEEDS_REVIEW,
    DRAFT_READY,
    ConversationThread,
    Draft,
    Interaction,
    OptOutRecord,
    SourceDocument,
)
from app.memory.repositories import DraftRepository, PostRepository
from app.memory.retrieval import HybridRetriever
from app.providers.base import LLMError, LLMMessage
from app.providers.factory import LLMService
from app.publishing.text_rules import (
    find_duplicate,
    normalize_typography,
    validate_text_rules,
)
from app.research.verification import ClaimVerifier, has_unlisted_quantitative_assertion
from app.schemas.content import (
    ContentGenerationError,
    ReplyDraftContent,
    VerificationReport,
)

logger = get_logger(__name__)


class ReplyDrafter:
    def __init__(
        self,
        settings: Settings,
        llm: LLMService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        embedder: EmbeddingProvider | None = None,
        clock=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.session_factory = session_factory
        self.embedder = embedder
        self.clock = clock or (lambda: datetime.now(UTC))
        self.verifier = ClaimVerifier(settings)

    async def _thread_context(
        self, session: AsyncSession, thread_id: int
    ) -> list[Interaction]:
        return list(
            (
                await session.scalars(
                    select(Interaction)
                    .where(Interaction.conversation_thread_id == thread_id)
                    .order_by(Interaction.occurred_at.desc())
                    .limit(25)
                )
            ).all()
        )

    async def draft_reply(self, account_id: int, inbound_post_id: str) -> Draft | None:
        async with self.session_factory() as session:
            interaction = await session.scalar(
                select(Interaction).where(
                    Interaction.account_id == account_id,
                    Interaction.x_post_id == inbound_post_id,
                    Interaction.direction == "inbound",
                )
            )
            if interaction is None:
                raise ValueError(
                    "no recorded inbound interaction for this post; "
                    "keyword-only or unsolicited replies are not drafted"
                )
            if interaction.author_x_user_id:
                opted_out = await session.scalar(
                    select(OptOutRecord).where(
                        OptOutRecord.account_id == account_id,
                        OptOutRecord.x_user_id == interaction.author_x_user_id,
                    )
                )
                if opted_out is not None:
                    logger.info(
                        "reply drafting skipped: opt-out",
                        extra={"extra_fields": {"post_id": inbound_post_id}},
                    )
                    return None
            thread = await session.get(ConversationThread, interaction.conversation_thread_id)
            if thread is None:
                raise ValueError("conversation thread missing for interaction")

            key = f"reply:{thread.id}:{inbound_post_id}"
            existing = await DraftRepository.get_by_key(session, account_id, key)
            if existing is not None:
                return existing

            persona = await load_persona(session, account_id)
            context_rows = await self._thread_context(session, thread.id)
            context_rows.sort(key=lambda row: row.occurred_at)
            formatted_lines = []
            for i, row in enumerate(context_rows, start=1):
                author = (
                    f"@{row.author_handle or row.author_x_user_id or 'unknown'}"
                    if row.direction == "inbound"
                    else f"@{self.settings.x_persona_handle or 'this_account'}"
                )
                is_target = row.x_post_id == inbound_post_id
                tag = (
                    "[🎯 TARGET TWEET TO REPLY TO]"
                    if is_target
                    else ("[ROOT POST]" if i == 1 else f"[THREAD MSG {i}]")
                )
                formatted_lines.append(f"{tag} {author}: {row.text}")
            context_text = "\n".join(formatted_lines)
            retriever = HybridRetriever(self.settings, self.embedder)
            try:
                memory = await retriever.retrieve(
                    session,
                    account_id=account_id,
                    query=interaction.text[:400],
                    categories=("conversation", "semantic", "episodic"),
                    conversation_thread_id=thread.id,
                    limit=3,
                )
                memory_context = memory.as_prompt_context(max_chars_per_memory=400)
            except Exception as exc:
                logger.warning("reply memory retrieval failed",
                               extra={"extra_fields": {"error": str(exc)[:200]}})
                memory_context = []

            recent_posts = await PostRepository.recent(
                session, account_id, limit=self.settings.recent_posts_for_dedup
            )
            recent_texts = [post.text for post in recent_posts]

            messages = build_reply_messages(
                persona=persona,
                conversation_context=context_text,
                memories=memory_context,
                now=self.clock(),
                handle=interaction.author_handle,
                max_post_length=self.settings.max_post_length,
                target_post=interaction.text,
                recent_posts=recent_texts[:5],
            )
            try:
                content, _ = await self.llm.generate_structured(
                    messages, ReplyDraftContent, operation="draft_reply"
                )
            except LLMError as exc:
                raise ContentGenerationError(f"reply drafting failed: {exc}") from exc

            if not content.send_recommended:
                interaction.metadata_ = {
                    **(interaction.metadata_ or {}),
                    "no_reply_recommended": True,
                    "evaluated_at": self.clock().isoformat(),
                }
                await session.commit()
                return None

            parts = [normalize_typography(part) for part in content.parts if part.strip()]
            parts = [part for part in parts if part]
            text_rules = validate_text_rules(self.settings, parts, format="hot_take")
            source_ids = list((thread.state or {}).get("source_document_ids", []))
            documents: list[SourceDocument] = []
            if source_ids:
                documents = list(
                    (
                        await session.scalars(
                            select(SourceDocument).where(
                                SourceDocument.account_id == account_id,
                                SourceDocument.id.in_(source_ids),
                            )
                        )
                    ).all()
                )
            verification = self.verifier.verify(content.claims, documents, now=self.clock())

            joined = "\n\n".join(parts)
            novelty = await retriever.novelty_score(
                session, account_id=account_id, text=joined
            )
            duplicate = find_duplicate(
                text=joined,
                recent_texts=recent_texts,
                settings=self.settings,
                embedding_similarity=None if novelty is None else max(0.0, 1.0 - novelty),
            )

            blocking: list[str] = list(text_rules.issues) + list(duplicate.reasons)
            if not verification.passed:
                blocking.extend(verification.issues)
            if has_unlisted_quantitative_assertion(joined, content.claims):
                blocking.append("reply contains quantitative claims without declared evidence")

            draft = await DraftRepository.create(
                session,
                account_id=account_id,
                kind="reply",
                status=DRAFT_BLOCKED if blocking else DRAFT_NEEDS_REVIEW,
                idempotency_key=key,
                topic=f"reply to @{interaction.author_handle or interaction.author_x_user_id}",
                body=parts[0] if parts else "",
                thread_parts=parts[1:],
                tone="hot_take",
                rationale=content.rationale,
                conversation_thread_id=thread.id,
                in_reply_to_post_id=interaction.x_post_id,
                target_author_id=interaction.author_x_user_id,
                target_author_handle=interaction.author_handle,
                verification_report=verification.model_dump(mode="json"),
                policy_report={
                    "duplicate": duplicate.model_dump(mode="json"),
                    "text_rules": text_rules.model_dump(mode="json"),
                    "opt_out": False,
                    "inbound_interaction": True,
                    "conversation_context": bool(context_text.strip()),
                    "send_recommended": content.send_recommended,
                    "blocking": blocking,
                },
                error="; ".join(blocking)[:4000] if blocking else None,
            )
            interaction.metadata_ = {
                **(interaction.metadata_ or {}),
                "reply_draft_id": draft.id,
                "evaluated_at": self.clock().isoformat(),
            }
            await session.commit()
            logger.info(
                "reply draft persisted",
                extra={"extra_fields": {"draft_id": draft.id, "status": draft.status,
                                        "blocked": len(blocking)}},
            )
            return draft

    async def verification_for(self, draft: Draft) -> VerificationReport:
        return VerificationReport.model_validate(draft.verification_report or {"passed": False})

    async def generate_manual_reply(
        self,
        account_id: int,
        post_text: str,
        *,
        post_url: str = "",
        conversation_context: str = "",
        preferred_style: str = "analysis",
    ) -> dict[str, Any]:
        """Generate context-aware geopolitical reply suggestions for operator manual posting."""
        clean_text = post_text.strip()
        if not clean_text:
            raise ValueError("post_text cannot be empty")

        target_post_id = None
        target_handle = None
        if post_url:
            match = re.search(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)", post_url)
            if match:
                target_handle = match.group(1)
                target_post_id = match.group(2)

        system_prompt = (
            "You are @ssh_bugs on X, an engaging and observant Indian commentator.\n"
            "You are drafting 3 sharp, easy-to-understand reply suggestions to a specific tweet on X.\n\n"
            "RULES FOR EASY ENGLISH & HIGH ENGAGEMENT:\n"
            "1. SUPER SIMPLE ENGLISH: Plain, everyday words. Write like a real person in a group chat, not an essay or robot.\n"
            "2. HIGH ENGAGEMENT: Add real value, call out ground realities, or highlight the biggest irony.\n"
            "3. NO HASHTAGS: Never include hashtags.\n"
            "4. NO AI FLUFF: Avoid 'in my opinion', 'it is important to note', 'delve into'. Speak directly with conviction.\n"
            "5. LENGTH: 120 to 220 characters for each reply. Twitter limit is 280.\n"
            "6. Generate 3 distinct reply styles:\n"
            "   - STYLE_A (Sharp Point): Cut straight to the real motive or economic fact.\n"
            "   - STYLE_B (Ground Reality): Call out what actually happens on the ground in India.\n"
            "   - STYLE_C (Direct Take): A punchy, clever one-liner cutting through the PR or spin.\n\n"
            "OUTPUT FORMAT:\n"
            "STYLE_A: <text under 220 chars>\n"
            "STYLE_B: <text under 220 chars>\n"
            "STYLE_C: <text under 220 chars>"
        )

        user_prompt = (
            f"TARGET TWEET TO REPLY TO:\n\"\"\"{clean_text}\"\"\"\n\n"
            + (f"TARGET AUTHOR: @{target_handle}\n\n" if target_handle else "")
            + (f"CONVERSATION / THREAD CONTEXT:\n\"\"\"{conversation_context.strip()}\"\"\"\n\n" if conversation_context.strip() else "")
            + "Draft the 3 distinct Indian commentary reply suggestions now:"
        )

        resp = await self.llm.generate(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
            ],
            operation="manual_reply_generation",
            temperature=0.7,
            max_tokens=350,
        )

        raw = resp.text.strip()
        parsed: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            for key in ("STYLE_A:", "STYLE_B:", "STYLE_C:"):
                if line.upper().startswith(key):
                    val = line[len(key):].strip().strip('"').strip("'")
                    parsed[key[:7]] = val

        if not parsed:
            lines = [line_text.strip().strip('"').strip("'") for line_text in raw.splitlines() if len(line_text.strip()) > 10]
            if lines:
                parsed["STYLE_A"] = lines[0]
            if len(lines) > 1:
                parsed["STYLE_B"] = lines[1]
            if len(lines) > 2:
                parsed["STYLE_C"] = lines[2]

        styles_meta = [
            ("STYLE_A", "analysis", "Strategic Analysis", "Geopolitical motive and strategic leverage"),
            ("STYLE_B", "context", "Strategic Context", "Historical background and ripple effects"),
            ("STYLE_C", "punchy_take", "Direct Take", "Sharp, punchy foreign policy take"),
        ]

        now = self.clock()
        suggestions = []
        async with self.session_factory() as session:
            for code, format_tag, label, desc in styles_meta:
                text_content = parsed.get(code, "").strip()
                if not text_content:
                    continue
                if len(text_content) > 280:
                    text_content = text_content[:277] + "..."
                text_content = normalize_typography(text_content)

                intent_query = quote(text_content)
                intent_url = f"https://x.com/intent/post?text={intent_query}"
                if target_post_id:
                    intent_url += f"&in_reply_to={target_post_id}"

                key = f"manual_reply:{account_id}:{abs(hash(text_content)) % 100000000}:{int(now.timestamp())}"
                draft = await DraftRepository.create(
                    session,
                    account_id=account_id,
                    kind="reply",
                    status=DRAFT_READY,
                    idempotency_key=key,
                    topic=f"Reply to {('@' + target_handle) if target_handle else 'post'}: {clean_text[:60]}",
                    body=text_content,
                    thread_parts=[],
                    tone=format_tag,
                    rationale=desc,
                    in_reply_to_post_id=target_post_id,
                    target_author_handle=target_handle,
                    verification_report={"passed": True, "claims": [], "unverified_facts": 0, "manual_workflow": True},
                    policy_report={
                        "manual_reply_workflow": True,
                        "format": format_tag,
                        "style_code": code,
                        "target_url": post_url,
                        "duplicate": {"duplicate": False, "method": "manual_workflow"},
                    },
                )
                await session.commit()
                await session.refresh(draft)

                suggestions.append({
                    "style": format_tag,
                    "code": code,
                    "label": label,
                    "description": desc,
                    "text": text_content,
                    "char_count": len(text_content),
                    "draft_id": draft.id,
                    "intent_url": intent_url,
                })

        return {
            "target_url": post_url,
            "target_post_id": target_post_id,
            "target_handle": target_handle,
            "target_text": clean_text,
            "suggestions": suggestions,
        }
