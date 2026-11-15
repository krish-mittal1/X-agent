"""Coordinated-behaviour guard.

Running more than one account makes a specific failure mode possible: an
operator's accounts follow each other, like each other's posts in bursts, and
touch the same targets minutes apart. X links accounts on exactly that pattern
and applies the restriction to both, so the guard rejects any planned action
that would look coordinated.

The rules are intentionally blunt and deterministic:

1. never act on the same target from two of our accounts inside the cooldown;
2. never touch one target more than N times a day;
3. keep same-kind actions spaced apart (follows cost more trust than likes);
4. no follow-then-like burst on one target from one account.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.config import Settings

ActionKind = Literal["post", "reply", "follow", "like"]

_KINDS: frozenset[str] = frozenset({"post", "reply", "follow", "like"})


@dataclass(frozen=True)
class GrowthAction:
    """A single planned or already-taken engagement action."""

    kind: ActionKind
    at: datetime
    account_id: int = 0
    target_handle: str = ""
    target_x_user_id: str = ""
    note: str = ""

    @property
    def target_key(self) -> str:
        return (self.target_x_user_id or self.target_handle or "").strip().lower()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "account_id": self.account_id,
            "target_handle": self.target_handle,
            "target_x_user_id": self.target_x_user_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class Rejection:
    action: GrowthAction
    rule: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.to_dict(),
            "rule": self.rule,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CoordinationReport:
    allowed: list[GrowthAction]
    rejected: list[Rejection]

    @property
    def passed(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": [action.to_dict() for action in self.allowed],
            "rejected": [rejection.to_dict() for rejection in self.rejected],
        }


def _spacing_seconds(settings: Settings, kind: str) -> float:
    if kind == "follow":
        return max(0.0, float(settings.growth_follow_min_seconds))
    if kind == "like":
        return max(0.0, float(settings.growth_like_min_seconds))
    if kind == "reply":
        return max(0.0, float(settings.growth_reply_min_seconds))
    return 0.0


def review_actions(
    *,
    planned: list[GrowthAction] | tuple[GrowthAction, ...],
    settings: Settings,
    now: datetime,
    history: list[GrowthAction] | tuple[GrowthAction, ...] = (),
) -> CoordinationReport:
    """Filter ``planned`` down to actions that cannot look coordinated.

    ``history`` is recent already-taken or already-planned actions, ordered or
    not; the guard sorts internally.
    """
    cooldown = timedelta(hours=max(0.0, float(settings.growth_cross_account_cooldown_hours)))
    burst = timedelta(seconds=max(0.0, float(settings.growth_burst_window_seconds)))
    per_target_cap = max(1, int(settings.growth_max_actions_per_target_per_day))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    rejected: list[Rejection] = []
    allowed: list[GrowthAction] = []

    # Rule 1a: dedupe repeats inside the plan itself.
    seen: set[tuple[str, str]] = set()
    candidates: list[GrowthAction] = []
    for action in sorted(planned, key=lambda item: item.at):
        if action.kind not in _KINDS:
            rejected.append(
                Rejection(action, "unknown_kind", f"unsupported action kind {action.kind!r}")
            )
            continue
        key = (action.kind, action.target_key)
        if action.target_key and key in seen:
            rejected.append(
                Rejection(
                    action, "duplicate_in_plan", f"already planned {action.kind} for this target"
                )
            )
            continue
        seen.add(key)
        candidates.append(action)

    prior = list(history)

    for action in candidates:
        target = action.target_key

        # Rule 1b: cross-account cooldown on a shared target.
        if target:
            foreign = [
                item
                for item in prior
                if item.target_key == target
                and item.account_id != action.account_id
                and abs(item.at - action.at) < cooldown
            ]
            if foreign:
                rejected.append(
                    Rejection(
                        action,
                        "cross_account_target",
                        f"another account touched @{target} inside the "
                        f"{cooldown.total_seconds() / 3600:.0f}h cooldown; "
                        "this is the pattern that links accounts",
                    )
                )
                continue

        # Rule 2: per-target daily cap (history + this plan).
        if target:
            same_target = [
                item
                for item in [*prior, *allowed]
                if item.target_key == target and item.at >= day_start
            ]
            if len(same_target) >= per_target_cap:
                rejected.append(
                    Rejection(
                        action,
                        "target_daily_cap",
                        f"@{target} already has {len(same_target)} action(s) today; "
                        f"cap is {per_target_cap}",
                    )
                )
                continue

        # Rule 3: same-kind spacing for this account.
        spacing = _spacing_seconds(settings, action.kind)
        if spacing > 0:
            same_kind = [
                item
                for item in [*prior, *allowed]
                if item.account_id == action.account_id and item.kind == action.kind
            ]
            too_close = [
                item
                for item in same_kind
                if timedelta(0) <= action.at - item.at < timedelta(seconds=spacing)
            ]
            if too_close:
                rejected.append(
                    Rejection(
                        action,
                        "kind_spacing",
                        f"{action.kind} only {int((action.at - too_close[-1].at).total_seconds())}s "
                        f"after the previous one; minimum is {int(spacing)}s",
                    )
                )
                continue

        # Rule 4: follow-then-like burst on the same target.
        if target and action.kind == "like":
            bursty = [
                item
                for item in [*prior, *allowed]
                if item.account_id == action.account_id
                and item.target_key == target
                and item.kind == "follow"
                and abs(action.at - item.at) <= burst
            ]
            if bursty:
                rejected.append(
                    Rejection(
                        action,
                        "follow_like_burst",
                        "liking a target immediately after following it reads as "
                        "scripted engagement",
                    )
                )
                continue

        allowed.append(action)

    return CoordinationReport(allowed=allowed, rejected=rejected)


def group_by_kind(actions: list[GrowthAction] | tuple[GrowthAction, ...]) -> dict[str, int]:
    """Small helper for plan summaries and logs."""
    counts: dict[str, int] = defaultdict(int)
    for action in actions:
        counts[action.kind] += 1
    return dict(counts)
