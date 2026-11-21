"""Account-age ramp for outbound volume.

A days-old account that posts hourly, follows in bursts and likes randomly is
treated as automation. The limits X applies to young accounts ease over roughly
two to four weeks of ordinary behaviour, so the agent's own limits follow the
same curve instead of running at adult volume from day one.

Three stages:

``warming``     age < ``growth_warmup_days``      - the safe floor
``ramping``     warmup <= age < established       - linear climb to target
``established`` age >= ``growth_established_days`` - operator's full targets
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import Settings


@dataclass(frozen=True)
class GrowthAllowance:
    """What this account is allowed to do today."""

    age_days: int
    stage: str
    posts_per_day: int
    replies_per_day: int
    follows_per_day: int
    likes_per_day: int
    threads_allowed: bool
    link_share: float
    require_media: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "age_days": self.age_days,
            "stage": self.stage,
            "posts_per_day": self.posts_per_day,
            "replies_per_day": self.replies_per_day,
            "follows_per_day": self.follows_per_day,
            "likes_per_day": self.likes_per_day,
            "threads_allowed": self.threads_allowed,
            "link_share": round(self.link_share, 3),
            "require_media": self.require_media,
            "notes": list(self.notes),
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _lerp(low: float, high: float, fraction: float) -> int:
    return int(round(low + (high - low) * _clamp(fraction, 0.0, 1.0)))


def compute_allowance(
    *,
    settings: Settings,
    account_created_at: datetime | None,
    now: datetime,
) -> GrowthAllowance:
    """Daily allowance for an account of this age.

    An unknown creation date is treated as brand new: the conservative stage is
    always the safe default when we cannot prove the account has aged.
    """
    warmup_days = max(1, int(settings.growth_warmup_days))
    established_days = max(warmup_days + 1, int(settings.growth_established_days))

    notes: list[str] = []
    if account_created_at is None:
        age_days = 0
        notes.append("account age unknown; using the warm-up stage")
    else:
        age_days = max(0, int((now - account_created_at).total_seconds() // 86400))

    warm_posts = max(0, int(settings.growth_warmup_posts_per_day))
    warm_replies = max(0, int(settings.growth_warmup_replies_per_day))
    warm_follows = max(0, int(settings.growth_warmup_follows_per_day))
    warm_likes = max(0, int(settings.growth_warmup_likes_per_day))

    target_posts = max(warm_posts, int(settings.posts_per_day_max))
    target_replies = max(warm_replies, int(settings.growth_target_replies_per_day))
    target_follows = max(warm_follows, int(settings.growth_target_follows_per_day))
    target_likes = max(warm_likes, int(settings.growth_target_likes_per_day))

    if age_days < warmup_days:
        stage = "warming"
        posts, replies = warm_posts, warm_replies
        follows, likes = warm_follows, warm_likes
        threads = False
        link_share = min(settings.growth_link_share, 0.2)
        notes.append(
            f"account is {age_days}d old; posting at warm-up volume until day {warmup_days}"
        )
    elif age_days < established_days:
        stage = "ramping"
        fraction = (age_days - warmup_days) / (established_days - warmup_days)
        posts = _lerp(warm_posts, target_posts, fraction)
        replies = _lerp(warm_replies, target_replies, fraction)
        follows = _lerp(warm_follows, target_follows, fraction)
        likes = _lerp(warm_likes, target_likes, fraction)
        threads = fraction >= 0.5
        link_share = settings.growth_link_share
        notes.append(f"ramping {fraction:.0%} toward full volume by day {established_days}")
    else:
        stage = "established"
        posts, replies = target_posts, target_replies
        follows, likes = target_follows, target_likes
        threads = True
        link_share = settings.growth_link_share
        notes.append("account age limit elapsed; running at configured volume")

    if age_days < established_days:
        posts_per_day = int(min(posts, settings.posts_per_day_max))
    else:
        posts_per_day = int(_clamp(posts, settings.posts_per_day_min, settings.posts_per_day_max))
    return GrowthAllowance(
        age_days=age_days,
        stage=stage,
        posts_per_day=int(posts_per_day),
        replies_per_day=max(0, replies),
        follows_per_day=max(0, follows),
        likes_per_day=max(0, likes),
        threads_allowed=threads,
        link_share=_clamp(link_share, 0.0, 1.0),
        require_media=bool(settings.growth_require_media),
        notes=tuple(notes),
    )
