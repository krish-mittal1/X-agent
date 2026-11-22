"""Concrete scheduled tasks. Every task is safe to re-run."""

from __future__ import annotations

import contextlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from app.agent.reply_drafter import ReplyDrafter
from app.agent.writer import ContentWriter
from app.analytics.metrics import AnalyticsService
from app.content_policy import is_political_content
from app.growth.commentary import assess_originality
from app.growth.pacing import (
    daily_slots,
    daily_target,
    next_slot,
    resolve_timezone,
    slot_due,
)
from app.growth.ramp import compute_allowance
from app.growth.service import GrowthService
from app.ingestion.x_research import XMentionsIngestor
from app.jobs.runner import JobContext
from app.logging_config import get_logger
from app.memory.consolidation import MemoryConsolidator
from app.memory.models import Account, Draft, Interaction, Post
from app.memory.repositories import AccountRepository, PostRepository, SourceRepository
from app.publishing.publisher import Publisher
from app.research.discovery import DiscoveryService
from app.research.llm_classifier import CandidateClassifier

logger = get_logger(__name__)


async def _default_account(context: JobContext) -> Account | None:
    async with context.session_factory() as session:
        return await AccountRepository.get_default(session, context.settings.x_persona_handle)


async def _discovery(context: JobContext, source_filter: set[str]) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    classifier = (
        CandidateClassifier(context.llm, context.settings) if context.llm.enabled else None
    )
    service = DiscoveryService(
        context.settings, context.session_factory, embedder=context.embedder,
        classifier=classifier,
    )
    stats = await service.run(account.id, source_filter=source_filter, score=False)
    return {
        "discovered": stats.ingested.stored,
        "fetched": stats.ingested.fetched,
        "duplicates": stats.ingested.duplicates,
        "errors": stats.errors[:5],
    }


async def rss_ingestion(context: JobContext) -> dict[str, Any]:
    return await _discovery(context, {"rss"})


async def github_discovery(context: JobContext) -> dict[str, Any]:
    return await _discovery(context, {"github"})


async def ranking(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    classifier = (
        CandidateClassifier(context.llm, context.settings) if context.llm.enabled else None
    )
    service = DiscoveryService(
        context.settings, context.session_factory, embedder=context.embedder,
        classifier=classifier,
    )
    stats = await service.run(account.id, ingest=False, score=True)
    return {"scored": stats.scored, "above_threshold": stats.above_threshold,
            "errors": stats.errors[:5]}


async def drafting(context: JobContext) -> dict[str, Any]:
    if not context.settings.drafting_enabled:
        return {"status": "skipped", "reason": "drafting disabled"}
    if not context.llm.enabled:
        return {"status": "skipped", "reason": "LLM unavailable"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    async with context.session_factory() as session:
        cutoff = datetime.now(UTC) - timedelta(hours=context.settings.freshness_window_hours)
        candidates = await SourceRepository.ranked_candidates(
            session,
            account.id,
            min_score=context.settings.min_candidate_score,
            limit=max(50, context.settings.max_candidates_per_run * 10),
            cutoff=cutoff,
        )
        if not context.settings.political_content_enabled:
            political = [
                item
                for item in candidates
                if is_political_content(item.title, item.excerpt)
            ]
            if political:
                await SourceRepository.mark_processed(
                    session, account.id, [item.id for item in political]
                )
                await session.commit()
            candidates = [item for item in candidates if item not in political]
        candidates = candidates[: context.settings.max_candidates_per_run]
    if not candidates:
        return {"drafts": 0, "reason": "no ranked candidates"}
    writer = ContentWriter(
        context.settings, context.llm, context.session_factory, embedder=context.embedder
    )
    outcomes = []
    for document in candidates[: context.settings.max_posts_per_run]:
        outcome = await writer.create_original_draft(account.id, [document.id])
        outcomes.append({"draft_id": outcome.draft_id, "status": outcome.status,
                         "blocked": outcome.blocked_reasons[:3]})
    return {"drafts": outcomes}


async def publishing(context: JobContext) -> dict[str, Any]:
    if not context.settings.publishing_enabled:
        return {"status": "skipped", "reason": "publishing disabled"}
    if context.settings.x_publishing_mode == "local_draft":
        return {"status": "skipped", "reason": "local_draft mode active"}
    if context.settings.emergency_pause:
        return {"status": "skipped", "reason": "emergency pause"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    cutoff = datetime.now(UTC) - timedelta(hours=context.settings.freshness_window_hours)
    async with context.session_factory() as session:
        ready = list(
            (
                await session.scalars(
                    select(Draft)
                    .where(
                        Draft.account_id == account.id,
                        Draft.kind == "original",
                        Draft.status == "ready",
                        Draft.created_at >= cutoff,
                    )
                    .order_by(Draft.created_at.desc())
                    .limit(context.settings.max_posts_per_run)
                )
            ).all()
        )
    if not ready:
        return {"published": 0, "reason": "no ready drafts"}

    # --- growth envelope -------------------------------------------------
    # Publishing only happens inside today's humanised slots, and never above
    # the volume this account's age allows. Without this the scheduler posts on
    # a fixed interval, which is the clearest automation signal an account can
    # emit.
    now = datetime.now(UTC)
    allowance = compute_allowance(
        settings=context.settings,
        account_created_at=getattr(account, "created_at", None),
        now=now,
    )
    zone = resolve_timezone(context.settings)
    day = now.astimezone(zone).date()
    target_posts = min(
        allowance.posts_per_day,
        daily_target(
            seed=account.id,
            day=day,
            minimum=context.settings.posts_per_day_min,
            maximum=context.settings.posts_per_day_max,
        ),
        context.settings.max_original_posts_per_day,
    )
    async with context.session_factory() as session:
        originals_today = await PostRepository.count_published_since(
            session, account.id, now - timedelta(days=1), kind="original"
        )
    if originals_today >= target_posts:
        return {
            "published": 0,
            "reason": "daily growth allowance reached",
            "stage": allowance.stage,
            "posts_today": originals_today,
            "target_posts": target_posts,
        }
    slots = daily_slots(
        day=day,
        target_posts=target_posts,
        window_start_hour=context.settings.posting_window_start_hour,
        window_end_hour=context.settings.posting_window_end_hour,
        min_gap_minutes=context.settings.growth_min_post_gap_minutes,
        seed=account.id,
        tz=zone,
    )
    if slots and slot_due(
        slots, now, tolerance_minutes=context.settings.growth_slot_tolerance_minutes
    ) is None:
        upcoming = next_slot(slots, now)
        return {
            "published": 0,
            "reason": "outside today's posting slots",
            "next_slot": upcoming.at.isoformat() if upcoming else None,
            "stage": allowance.stage,
        }

    publisher = Publisher(
        context.settings, context.session_factory, context.x_client,
        embedder=context.embedder,
    )
    results = []
    for draft in ready:
        policy_report = draft.policy_report or {}
        is_operator = bool(
            policy_report.get("source") == "operator_command"
            or policy_report.get("operator_approved")
            or policy_report.get("operator_override")
        )
        originality = assess_originality(
            text="\n\n".join([draft.body, *(draft.thread_parts or [])]),
            settings=context.settings,
            source_headline=draft.title or draft.topic or "",
            is_operator=is_operator,
        )
        if not originality.passed:
            # Remove it from the ready queue so the next slot can progress.
            async with context.session_factory() as session:
                stored = await session.get(Draft, draft.id)
                if stored is not None and stored.status == "ready":
                    stored.status = "needs_review"
                    stored.error = "; ".join(originality.issues)[:4000]
                    await session.commit()
            results.append({
                "draft_id": draft.id,
                "status": "held_for_originality",
                "reasons": list(originality.issues)[:3],
            })
            continue
        result = await publisher.publish_original(draft.id)
        results.append({"draft_id": draft.id, "status": result.status,
                        "reasons": result.reasons[:3]})
        if result.status in {"skipped", "unknown", "failed"}:
            break
    return {"results": results}


async def reply_drafts(context: JobContext) -> dict[str, Any]:
    if not context.settings.drafting_enabled:
        return {"status": "skipped", "reason": "drafting disabled"}
    if not context.llm.enabled:
        return {"status": "skipped", "reason": "LLM unavailable"}
    if context.settings.is_zero_cost_mode or not context.settings.enable_reply_scanner:
        return {
            "status": "skipped",
            "reason": "automated reply scanner disabled; operator manual reply workflow active",
        }
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    synced = 0
    if context.x_client is not None:
        ingestor = XMentionsIngestor(
            context.settings, context.session_factory, context.x_client,
            embedder=context.embedder,
        )
        if ingestor.enabled:
            synced = await ingestor.sync(account.id)
    async with context.session_factory() as session:
        candidates = list(
            (
                await session.scalars(
                    select(Interaction)
                    .where(
                        Interaction.account_id == account.id,
                        Interaction.direction == "inbound",
                        Interaction.occurred_at >= datetime.now(UTC) - timedelta(hours=48),
                    )
                    .order_by(Interaction.occurred_at.desc())
                    .limit(10)
                )
            ).all()
        )
        pending = [
            interaction
            for interaction in candidates
            if not (interaction.metadata_ or {}).get("reply_draft_id")
            and not (interaction.metadata_ or {}).get("no_reply_recommended")
        ][:3]
    drafter = ReplyDrafter(
        context.settings, context.llm, context.session_factory, embedder=context.embedder
    )
    drafted = []
    for interaction in pending:
        draft = await drafter.draft_reply(account.id, interaction.x_post_id)
        if draft is not None:
            drafted.append({"draft_id": draft.id, "status": draft.status})
    return {"synced_mentions": synced, "drafts": drafted}


async def consolidation(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    async with context.session_factory() as session:
        report = await MemoryConsolidator(context.settings).run(session, account.id)
        await session.commit()
    return report


async def analytics(context: JobContext) -> dict[str, Any]:
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    service = AnalyticsService(
        context.settings, context.session_factory, context.x_client
    )
    return await service.collect(account.id)


async def growth_plan(context: JobContext) -> dict[str, Any]:
    """Build and persist today's growth plan.

    Posts are scheduled by the humanised slots above; replies and follows are
    queued here for the operator (nothing is sent automatically), and the
    coordination guard has already removed anything that would link accounts.
    """
    if not context.settings.growth_enabled:
        return {"status": "skipped", "reason": "growth layer disabled"}
    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}
    service = GrowthService(context.settings, context.session_factory)
    plan = await service.build(account)
    queued = await service.persist(plan)
    return {
        "stage": plan.stage,
        "target_posts": plan.target_posts,
        "slots": [slot.at.isoformat() for slot in plan.slots],
        "reply_targets": [target.handle for target in plan.reply_targets],
        "follow_targets": plan.follow_targets,
        "queued_actions": queued,
        "rejected_actions": len(plan.rejected_actions),
        "link_budget": plan.link_budget,
        "notes": plan.notes[:5],
    }


async def outbound_engagement(context: JobContext) -> dict[str, Any]:
    """Autonomous Outbound Viral Engagement Worker.

    Scans rotating Indian viral accounts across non-political niches (fights/kalesh,
    cricket, memes, technology, finance, and pop culture), identifies fresh posts, generates
    high-impact takes, naturally likes the target post, and autonomously publishes
    replies and quote-tweets.
    """
    if (
        not getattr(context.settings, "outbound_engagement_enabled", True)
        or not context.settings.publishing_enabled
        or context.settings.dry_run
    ):
        return {
            "status": "skipped",
            "reason": "publishing or outbound engagement disabled / dry run",
        }
    if context.settings.emergency_pause:
        return {"status": "skipped", "reason": "emergency pause"}

    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}

    now = datetime.now(UTC)
    async with context.session_factory() as session:
        replies_today = await PostRepository.count_published_since(
            session, account.id, now - timedelta(days=1), kind="reply"
        )
    if replies_today >= context.settings.max_replies_per_day:
        return {
            "status": "skipped",
            "reason": f"daily reply limit reached ({replies_today}/{context.settings.max_replies_per_day})",
        }

    from app.growth.engagement import EngagementService, rank_timely_candidates, rotate_handle_batch
    from app.publishing.brave_cdp import BraveCDPPublisher, resolve_brave_executable

    publisher = BraveCDPPublisher(context.settings, context.session_factory)
    if not await publisher.is_cdp_available() and not resolve_brave_executable():
        return {"status": "skipped", "reason": "brave cdp unavailable"}

    service = EngagementService(
        context.settings,
        context.llm,
        context.session_factory,
        publisher=publisher,
    )

    target_handles = list(context.settings.viral_handles) or [
        # Kalesh, Fights & Viral Drama (लड़ाई)
        "gharkekalesh",
        "DeadlyKalesh",
        "KaleshChahiye",
        "crazyclips_",
        # Memes, Sarcasm & Desi Bakchodi
        "sagarcasm",
        "GabbbarSingh",
        "coolfunnytshirt",
        "Trend_ulkar",
        "memenist",
        "desimojito",
        "thetatva",
        "HasnaZarooriHai",
        "TupacShakur_IND",
        # Cricket & Sports Viral
        "mufaddal_vohra",
        "CricCrazyJohns",
        "Johns_Cricket",
        # Politics, Society & Public Absurdity
        "IndianTechGuide",
        "peakbengaluru",
        # Entertainment & Pop Culture
        "filmy_gyan",
        "tellychakkar",
    ]

    interval_seconds = max(
        60, int(context.settings.outbound_engagement_interval_minutes * 60)
    )
    interval_index = int(now.timestamp() // interval_seconds)
    target_handles = rotate_handle_batch(
        target_handles,
        batch_size=context.settings.outbound_handles_per_run,
        rotation_index=interval_index,
    )

    try:
        candidates = await service.scan_handles_for_candidates(target_handles, limit_per_handle=4)
    except Exception as exc:
        logger.warning(f"Failed scanning handles for candidates: {exc}")
        return {"status": "failed", "reason": f"handle scan error: {exc}"}

    if not candidates:
        return {
            "status": "skipped",
            "reason": (
                "no candidates met the observed-impressions threshold "
                f"({context.settings.engagement_min_impressions:,})"
            ),
        }

    if not context.settings.political_content_enabled:
        candidates = [
            candidate
            for candidate in candidates
            if not is_political_content(candidate.get("text", ""))
        ]
    if not candidates:
        return {"status": "skipped", "reason": "rotated handle batch had no non-political candidates"}

    # Filter out tweets and authors we already engaged with recently.
    async with context.session_factory() as session:
        existing_drafts = (
            await session.scalars(
                select(Draft).where(
                Draft.account_id == account.id,
                Draft.kind.in_(["reply", "original"]),
                )
            )
        ).all()
        engaged_ids: set[str] = set()
        recent_authors: set[str] = set()
        author_cutoff = now - timedelta(hours=context.settings.outbound_author_cooldown_hours)
        for existing in existing_drafts:
            report = existing.policy_report
            if isinstance(report, dict):
                tid = report.get("target_post_id")
                if tid:
                    engaged_ids.add(str(tid))
                author = str(report.get("target_author") or "").lstrip("@").lower()
                if author and existing.created_at >= author_cutoff:
                    recent_authors.add(author)

    fresh_candidates = [
        c
        for c in candidates
        if c.get("tweet_id")
        and str(c["tweet_id"]) not in engaged_ids
        and str(c.get("author") or "").lstrip("@").lower() not in recent_authors
    ]
    if not fresh_candidates:
        return {
            "status": "skipped",
            "reason": "all candidates or their authors are inside the engagement cooldown",
        }

    timely_candidates = rank_timely_candidates(
        fresh_candidates, now=now, max_age_hours=context.settings.engagement_max_age_hours
    )
    if not timely_candidates:
        return {"status": "skipped", "reason": "no recent engagement candidates"}
    chosen = timely_candidates[0]
    target_url = chosen["url"]
    target_text = chosen["text"]
    target_author = chosen["author"]

    try:
        bangers = await service.generate_bangers(target_text, target_author=target_author)
    except Exception as exc:
        logger.warning(f"Failed generating bangers for target: {exc}")
        return {"status": "failed", "reason": f"generation error: {exc}"}

    # Alternate deterministically by engagement interval, not wall-clock hour.
    # A four-hour interval can otherwise land on the same hour parity forever.
    action: Literal["reply", "quote"] = "quote" if interval_index % 2 else "reply"

    if action == "quote":
        style = "quote_tweet"
        chosen_content = bangers.get("quote_tweet") or bangers.get("sarcastic", "")
    else:
        styles_pool = ["value_add", "strong_take", "bakchodi", "sarcastic"]
        style = styles_pool[(interval_index // 2) % len(styles_pool)]
        chosen_content = (
            bangers.get(style) or bangers.get("sarcastic") or bangers.get("value_add", "")
        )

    if not chosen_content:
        return {"status": "skipped", "reason": "failed to generate banger content"}

    draft = await service.create_engagement_draft(
        account_id=account.id,
        target_post_url=target_url,
        content=chosen_content,
        action=action,
        style=style,
        operator_approved=True,
    )

    try:
        res = await service.publish_engagement(draft)
        logger.info(
            "Autonomous outbound engagement dispatched",
            extra={
                "extra_fields": {
                    "target_url": target_url,
                    "action": action,
                    "status": res.status,
                    "post_ids": res.post_ids,
                }
            },
        )
        return {
            "status": res.status,
            "action": action,
            "style": style,
            "target_url": target_url,
            "author": target_author,
            "content": chosen_content,
            "post_ids": res.post_ids,
            "draft_id": draft.id,
        }
    except Exception as exc:
        logger.exception(
            "Failed publishing outbound engagement",
            extra={"extra_fields": {"draft_id": draft.id, "error": str(exc)}},
        )
        return {
            "status": "failed",
            "action": action,
            "target_url": target_url,
            "reason": str(exc),
            "draft_id": draft.id,
        }


async def viral_video_repost(context: JobContext) -> dict[str, Any]:
    """Viral Video Repost Engine.

    Finds high-reach tweets with embedded videos from top Indian viral handles,
    downloads the video via yt-dlp, extracts keyframes, generates a fresh banger
    caption via Gemini Vision, and posts the video to @ssh_bugs with the new caption.

    This creates original-looking content that piggybacks on viral video reach
    without infringing on platform rules (video is downloaded and re-posted as
    original content, not retweet/quote).
    """
    if (
        not context.settings.publishing_enabled
        or context.settings.dry_run
    ):
        return {"status": "skipped", "reason": "publishing disabled / dry run"}
    if not getattr(context.settings, "viral_video_repost_enabled", True):
        return {"status": "skipped", "reason": "viral video repost disabled"}
    if context.settings.emergency_pause:
        return {"status": "skipped", "reason": "emergency pause"}
    if not context.settings.viral_video_rights_confirmed:
        return {
            "status": "skipped",
            "reason": "video download/reupload requires confirmed reuse rights",
        }

    account = await _default_account(context)
    if account is None:
        return {"status": "skipped", "reason": "no active account"}

    # Check daily original post limit
    now = datetime.now(UTC)
    async with context.session_factory() as session:
        posts_today = await PostRepository.count_published_since(
            session, account.id, now - timedelta(days=1), kind="original"
        )
    if posts_today >= context.settings.max_original_posts_per_day:
        return {
            "status": "skipped",
            "reason": f"daily original post limit reached ({posts_today}/{context.settings.max_original_posts_per_day})",
        }

    async with context.session_factory() as session:
        video_posts_today = int(
            await session.scalar(
                select(func.count(Post.id))
                .join(Draft, Post.draft_id == Draft.id)
                .where(
                    Post.account_id == account.id,
                    Post.published_at >= now - timedelta(days=1),
                    Draft.policy_report["source"].astext == "video_repost_engine",
                )
            )
            or 0
        )
    if video_posts_today >= context.settings.max_video_reposts_per_day:
        return {
            "status": "skipped",
            "reason": (
                "daily video repost limit reached "
                f"({video_posts_today}/{context.settings.max_video_reposts_per_day})"
            ),
        }

    from app.growth.video_repost import (
        _extract_keyframes,
        download_tweet_video,
        generate_video_caption,
        post_video_with_caption,
        scan_viral_handles_for_video_posts,
    )

    target_handles = list(context.settings.viral_handles) or [
        # Kalesh, Street Drama & Fights (लड़ाई)
        "gharkekalesh",
        "DeadlyKalesh",
        "KaleshChahiye",
        "crazyclips_",
        # Viral Memes & Absurdity
        "sagarcasm",
        "GabbbarSingh",
        "memenist",
        "thetatva",
        # Cricket & Sports Highlights
        "mufaddal_vohra",
        "CricCrazyJohns",
        # Tech & City Chaos
        "IndianTechGuide",
        "peakbengaluru",
        # Pop Culture & Viral Clips
        "filmy_gyan",
    ]

    try:
        candidates = await scan_viral_handles_for_video_posts(
            context.settings,
            target_handles,
            limit_per_handle=2,
            min_impressions=getattr(context.settings, "outbound_min_impressions", 10_000),
        )
    except Exception as exc:
        logger.warning(f"Video scan failed: {exc}")
        return {"status": "failed", "reason": f"scan error: {exc}"}

    if not candidates:
        return {"status": "skipped", "reason": "no viral video candidates found"}

    if not context.settings.political_content_enabled:
        candidates = [
            candidate
            for candidate in candidates
            if not is_political_content(candidate.get("text", ""))
        ]
    if not candidates:
        return {
            "status": "skipped",
            "reason": "video candidates were excluded by the non-political topic boundary",
        }

    # Filter out tweets we already reposted or engaged with
    async with context.session_factory() as session:
        all_draft_reports = (
            await session.scalars(
                select(Draft.policy_report).where(
                    Draft.account_id == account.id,
                )
            )
        ).all()
        already_used: set[str] = set()
        for report in all_draft_reports:
            if isinstance(report, dict) and report.get("source") == "video_repost_engine":
                src = report.get("source_tweet_url")
                if src:
                    already_used.add(src)
                tid = report.get("source_tweet_id")
                if tid:
                    already_used.add(str(tid))

    fresh_candidates = [
        c for c in candidates
        if c.get("url") not in already_used and str(c.get("tweet_id", "")) not in already_used
    ]
    if not fresh_candidates:
        return {"status": "skipped", "reason": "all viral video candidates already reposted"}

    chosen = fresh_candidates[0]
    target_url = chosen["url"]
    tweet_text = chosen.get("text", "")
    tweet_author = chosen.get("author", "")

    logger.info(
        "Viral video candidate selected",
        extra={
            "extra_fields": {
                "url": target_url,
                "impressions": chosen.get("impressions", 0),
                "author": tweet_author,
            }
        },
    )

    # Download video to a temp directory
    with tempfile.TemporaryDirectory(prefix="xagent_video_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        auth_token = (
            context.settings.x_auth_token.get_secret_value()
            if context.settings.x_auth_token
            else None
        )
        ct0 = (
            context.settings.x_ct0.get_secret_value()
            if context.settings.x_ct0
            else None
        )

        try:
            video_file = await download_tweet_video(
                target_url,
                tmp_path,
                auth_token=auth_token,
                ct0=ct0,
                settings=context.settings,
            )
        except Exception as exc:
            logger.warning(f"Video download failed: {exc}")
            return {"status": "failed", "reason": f"download error: {exc}"}

        if video_file is None:
            return {"status": "skipped", "reason": "yt-dlp could not download video from tweet"}

        # Extract keyframes for Gemini Vision
        keyframes: list[bytes] = []
        with contextlib.suppress(Exception):
            keyframes = _extract_keyframes(video_file)

        logger.info(
            f"Extracted {len(keyframes)} keyframes from video",
            extra={"extra_fields": {"video_path": str(video_file)}},
        )

        # Generate fresh caption via Gemini Vision
        try:
            caption = await generate_video_caption(
                context.llm,
                tweet_text,
                keyframes,
                tweet_author=tweet_author,
            )
        except Exception as exc:
            logger.warning(f"Caption generation failed: {exc}")
            return {"status": "failed", "reason": f"caption generation error: {exc}"}

        logger.info(
            "Generated video caption",
            extra={"extra_fields": {"caption": caption, "source": target_url}},
        )

        # Post with video
        try:
            result = await post_video_with_caption(
                context.settings,
                context.session_factory,
                video_file,
                caption,
                account.id,
                target_url,
            )
        except Exception as exc:
            logger.exception(f"Video post dispatch failed: {exc}")
            return {"status": "failed", "reason": f"post error: {exc}"}

    return {
        "status": result.get("status", "unknown"),
        "caption": caption,
        "source_tweet_url": target_url,
        "source_author": tweet_author,
        "keyframes_used": len(keyframes),
        "post_ids": result.get("post_ids", []),
    }


TASKS = {
    "growth_plan": growth_plan,
    "rss_ingestion": rss_ingestion,
    "github_discovery": github_discovery,
    "ranking": ranking,
    "drafting": drafting,
    "publishing": publishing,
    "outbound_engagement": outbound_engagement,
    "reply_drafts": reply_drafts,
    "memory_consolidation": consolidation,
    "analytics": analytics,
    "viral_video_repost": viral_video_repost,
}

__all__ = ["TASKS"]
