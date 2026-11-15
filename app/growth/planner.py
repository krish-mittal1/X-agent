"""Daily growth plan: what to post, where to reply, whom to follow.

The plan is derived, never guessed: account age gives the allowance, the
allowance plus the operator's range gives the posting target, pacing turns the
target into irregular slots, and the coordination guard strips anything that
would link this account to another of the operator's accounts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo

from app.config import Settings
from app.growth.alts import GrowthAction, Rejection, review_actions
from app.growth.pacing import (
    PostingSlot,
    daily_slots,
    daily_target,
    engagement_times,
    resolve_timezone,
)
from app.growth.ramp import GrowthAllowance, compute_allowance


@dataclass(frozen=True)
class ReplyTarget:
    """A conversation partner worth spending one of today's replies on."""

    handle: str
    x_user_id: str = ""
    followers: int | None = None
    score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "x_user_id": self.x_user_id,
            "followers": self.followers,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DailyGrowthPlan:
    account_id: int
    day: date
    timezone: str
    stage: str
    allowance: GrowthAllowance
    target_posts: int
    slots: list[PostingSlot] = field(default_factory=list)
    reply_targets: list[ReplyTarget] = field(default_factory=list)
    follow_targets: list[str] = field(default_factory=list)
    actions: list[GrowthAction] = field(default_factory=list)
    rejected_actions: list[Rejection] = field(default_factory=list)
    link_budget: int = 0
    media_required: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "day": self.day.isoformat(),
            "timezone": self.timezone,
            "stage": self.stage,
            "allowance": self.allowance.to_dict(),
            "target_posts": self.target_posts,
            "slots": [slot.to_dict() for slot in self.slots],
            "reply_targets": [target.to_dict() for target in self.reply_targets],
            "follow_targets": list(self.follow_targets),
            "actions": [action.to_dict() for action in self.actions],
            "rejected_actions": [item.to_dict() for item in self.rejected_actions],
            "link_budget": self.link_budget,
            "media_required": self.media_required,
            "notes": list(self.notes),
        }


def _is_mid_size(target: ReplyTarget, settings: Settings) -> tuple[bool, str]:
    if target.followers is None:
        return True, "follower count unknown; included from an inbound conversation"
    if target.followers < settings.growth_reply_min_followers:
        return False, f"{target.followers} followers is below the mid-size floor"
    if target.followers > settings.growth_reply_max_followers:
        return False, f"{target.followers} followers is above the mid-size ceiling"
    return True, "in the mid-size band"


def build_plan(
    *,
    settings: Settings,
    account_id: int,
    account_created_at: datetime | None,
    now: datetime,
    reply_candidates: list[ReplyTarget] | tuple[ReplyTarget, ...] = (),
    follow_candidates: list[str] | tuple[str, ...] = (),
    recent_actions: list[GrowthAction] | tuple[GrowthAction, ...] = (),
    link_posts_today: int = 0,
    tz: tzinfo | None = None,
) -> DailyGrowthPlan:
    """Assemble today's plan for one account.

    Every number here is bounded by the age allowance, so a new account cannot
    be scheduled into adult volume even if the operator's targets say otherwise.
    """
    zone = tz or resolve_timezone(settings)
    local_now = now.astimezone(zone)
    day = local_now.date()
    allowance = compute_allowance(settings=settings, account_created_at=account_created_at, now=now)
    notes = list(allowance.notes)

    desired = daily_target(
        seed=account_id,
        day=day,
        minimum=settings.posts_per_day_min,
        maximum=settings.posts_per_day_max,
    )
    target_posts = max(0, min(desired, allowance.posts_per_day))
    capped = max(0, int(settings.max_original_posts_per_day))
    if target_posts > capped:
        notes.append(f"posting target reduced to {capped} by MAX_ORIGINAL_POSTS_PER_DAY")
        target_posts = capped

    slots = daily_slots(
        day=day,
        target_posts=target_posts,
        window_start_hour=settings.posting_window_start_hour,
        window_end_hour=settings.posting_window_end_hour,
        min_gap_minutes=settings.growth_min_post_gap_minutes,
        seed=account_id,
        tz=zone,
    )

    # --- reply targets -------------------------------------------------
    selected: list[ReplyTarget] = []
    skipped: list[str] = []
    for candidate in sorted(reply_candidates, key=lambda item: item.score, reverse=True):
        ok, reason = _is_mid_size(candidate, settings)
        if not ok:
            skipped.append(f"@{candidate.handle}: {reason}")
            continue
        if any(
            item.kind == "reply"
            and item.target_handle.lstrip("@").lower() == candidate.handle.lstrip("@").lower()
            for item in recent_actions
        ):
            skipped.append(f"@{candidate.handle}: replied recently")
            continue
        selected.append(candidate)
    selected = selected[: allowance.replies_per_day]
    if skipped:
        notes.append("reply targets filtered: " + "; ".join(skipped[:4]))

    reply_times = engagement_times(
        day=day,
        count=len(selected),
        window_start_hour=settings.posting_window_start_hour,
        window_end_hour=settings.posting_window_end_hour,
        min_gap_minutes=settings.growth_reply_min_gap_minutes,
        seed=account_id,
        tz=zone,
    )

    # --- follow targets ------------------------------------------------
    already_touched = {
        item.target_handle.lstrip("@").lower() for item in recent_actions if item.kind == "follow"
    }
    follow_list: list[str] = []
    for handle in follow_candidates:
        clean = handle.strip().lstrip("@")
        if not clean or clean.lower() in already_touched:
            continue
        if clean.lower() in {existing.lstrip("@").lower() for existing in follow_list}:
            continue
        follow_list.append(clean)
        if len(follow_list) >= allowance.follows_per_day:
            break
    follow_times = engagement_times(
        day=day,
        count=len(follow_list),
        window_start_hour=settings.posting_window_start_hour,
        window_end_hour=settings.posting_window_end_hour,
        min_gap_minutes=settings.growth_follow_min_gap_minutes,
        seed=f"follow:{account_id}",
        tz=zone,
    )

    planned: list[GrowthAction] = []
    for target, at in zip(selected, reply_times, strict=False):
        planned.append(
            GrowthAction(
                kind="reply",
                at=at,
                account_id=account_id,
                target_handle=target.handle,
                target_x_user_id=target.x_user_id,
                note=target.reason,
            )
        )
    for handle, at in zip(follow_list, follow_times, strict=False):
        planned.append(
            GrowthAction(
                kind="follow",
                at=at,
                account_id=account_id,
                target_handle=handle,
                note="relevant account in our beat",
            )
        )

    report = review_actions(planned=planned, settings=settings, now=now, history=recent_actions)
    if report.rejected:
        notes.append(f"{len(report.rejected)} planned action(s) rejected by the coordination guard")

    link_budget = int(round(target_posts * allowance.link_share))
    link_budget = max(0, min(target_posts, link_budget))
    if link_posts_today >= link_budget and target_posts:
        notes.append(
            f"link budget spent ({link_posts_today}/{link_budget}); next posts should be "
            "text or image only"
        )

    return DailyGrowthPlan(
        account_id=account_id,
        day=day,
        timezone=str(getattr(zone, "key", zone)),
        stage=allowance.stage,
        allowance=allowance,
        target_posts=target_posts,
        slots=slots,
        reply_targets=selected,
        follow_targets=follow_list,
        actions=report.allowed,
        rejected_actions=report.rejected,
        link_budget=link_budget,
        media_required=allowance.require_media,
        notes=notes,
    )
