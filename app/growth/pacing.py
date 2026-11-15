"""Humanised posting and engagement timing.

X's reach classifiers treat metronomic posting as automation: the same minute of
every hour, every hour, is the single clearest automation signal an account can
emit. These helpers produce schedules that are:

* **irregular** - every gap is different, drawn from a random weight vector, so
  two consecutive posts are never the same distance apart;
* **deterministic per account-day** - the same account on the same local day
  always gets the same slots, so scheduled jobs stay idempotent and a restart
  cannot double-post;
* **bounded** - slots never leave the operator's waking window and never sit
  closer together than the configured floor.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings

_UTC = ZoneInfo("UTC")
_MAX_SLOTS_PER_DAY = 24


@dataclass(frozen=True)
class PostingSlot:
    """One planned publishing moment inside the operator's day."""

    index: int
    at: datetime

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "at": self.at.isoformat()}


def resolve_timezone(settings: Settings) -> tzinfo:
    """Return the operator's timezone, falling back to UTC when unset/unknown."""
    name = (settings.timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _UTC


def _rng(*parts: object) -> random.Random:
    material = ":".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def daily_target(*, seed: str | int, day: date, minimum: int, maximum: int) -> int:
    """Posts for one day, drawn from the operator's range.

    A fixed 4-every-day pattern is itself a signal, so the daily count varies
    inside ``[minimum, maximum]``. Large accounts that actually post daily all
    drift; the range is what makes the drift plausible.
    """
    low, high = sorted((max(0, minimum), max(0, maximum)))
    if high == 0:
        return 0
    return _rng("target", seed, day.isoformat()).randint(low, high)


def daily_slots(
    *,
    day: date,
    target_posts: int,
    window_start_hour: int,
    window_end_hour: int,
    min_gap_minutes: float,
    seed: str | int,
    tz: tzinfo | None = None,
) -> list[PostingSlot]:
    """Return irregular, minimum-spaced publishing slots for ``day``.

    The window is split into ``target_posts + 1`` random weights; those weights
    become the leading, interleaved and trailing gaps. Because the floor gap is
    subtracted before the free time is distributed, the minimum spacing holds by
    construction while the visible gaps stay uneven.
    """
    zone = tz or _UTC
    count = min(max(0, int(target_posts)), _MAX_SLOTS_PER_DAY)
    if count == 0:
        return []
    start_hour = min(max(int(window_start_hour), 0), 23)
    end_hour = min(max(int(window_end_hour), start_hour + 1), 24)
    start = datetime.combine(day, time(hour=start_hour), tzinfo=zone)
    end = datetime.combine(day, time(hour=end_hour % 24), tzinfo=zone)
    if end_hour == 24:
        end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
    window_minutes = (end - start).total_seconds() / 60.0
    floor = max(0.0, float(min_gap_minutes))

    if window_minutes <= 0:
        return []
    # Shrink the floor if the operator asks for more posts than the window holds.
    max_floor = max(0.0, (window_minutes - count) / max(1, count - 1))
    floor = min(floor, max_floor)
    reserve = floor * (count - 1)
    free = max(0.0, window_minutes - reserve)

    rng = _rng("slots", seed, day.isoformat(), count, start_hour, end_hour)
    weights = [rng.uniform(0.3, 1.0) for _ in range(count + 1)]
    total = sum(weights) or 1.0
    gaps = [weight / total * free for weight in weights]

    slots: list[PostingSlot] = []
    cursor = start + timedelta(minutes=gaps[0])
    for index in range(count):
        slots.append(PostingSlot(index=index, at=cursor))
        cursor = cursor + timedelta(minutes=floor + gaps[index + 1])
    return slots


def engagement_times(
    *,
    day: date,
    count: int,
    window_start_hour: int,
    window_end_hour: int,
    min_gap_minutes: float,
    seed: str | int,
    tz: tzinfo | None = None,
) -> list[datetime]:
    """Timing for replies/follows: same rules, separate random stream."""
    return [
        slot.at
        for slot in daily_slots(
            day=day,
            target_posts=count,
            window_start_hour=window_start_hour,
            window_end_hour=window_end_hour,
            min_gap_minutes=min_gap_minutes,
            seed=f"engagement:{seed}",
            tz=tz,
        )
    ]


def slot_due(
    slots: list[PostingSlot] | tuple[PostingSlot, ...],
    now: datetime,
    *,
    tolerance_minutes: float,
) -> PostingSlot | None:
    """The slot that should publish right now, if any.

    ``tolerance_minutes`` bounds how late a slot may still fire: a job that wakes
    two hours after a slot missed it, and posting then would put us back on an
    irregular-but-not-human timeline.
    """
    tolerance = timedelta(minutes=max(0.0, float(tolerance_minutes)))
    for slot in slots:
        if slot.at <= now <= slot.at + tolerance:
            return slot
    return None


def next_slot(
    slots: list[PostingSlot] | tuple[PostingSlot, ...], now: datetime
) -> PostingSlot | None:
    """The first slot still in the future (used for human-readable skip reasons)."""
    for slot in slots:
        if slot.at > now:
            return slot
    return None
