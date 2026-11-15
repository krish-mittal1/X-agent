"""Unit tests for the growth layer (no database, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.growth.alts import GrowthAction, review_actions
from app.growth.commentary import assess_originality
from app.growth.pacing import daily_slots, daily_target, slot_due
from app.growth.planner import ReplyTarget, build_plan
from app.growth.ramp import compute_allowance


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "llm_provider": "disabled",
        "log_json": False,
        "x_publishing_mode": "local_draft",
        "publishing_enabled": False,
        "dry_run": True,
        "timezone": "UTC",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


# --- pacing -------------------------------------------------------------
def test_slots_stay_inside_the_window_and_respect_the_floor() -> None:
    slots = daily_slots(
        day=date(2026, 11, 12),
        target_posts=4,
        window_start_hour=8,
        window_end_hour=23,
        min_gap_minutes=55,
        seed=7,
    )
    assert len(slots) == 4
    assert all(slot.at.hour >= 8 for slot in slots)
    assert all(slot.at < datetime(2026, 11, 13, 0, 0, tzinfo=UTC) for slot in slots)
    gaps = [
        (later.at - earlier.at).total_seconds() / 60
        for earlier, later in zip(slots, slots[1:], strict=False)
    ]
    assert all(gap >= 54.99 for gap in gaps), gaps
    # Irregular: the point is that no two gaps look the same.
    assert len({round(gap, 1) for gap in gaps}) == len(gaps)


def test_slots_are_deterministic_per_day_but_differ_between_days() -> None:
    kwargs = {
        "target_posts": 3,
        "window_start_hour": 9,
        "window_end_hour": 21,
        "min_gap_minutes": 45,
        "seed": 42,
    }
    first = daily_slots(day=date(2026, 11, 12), **kwargs)
    again = daily_slots(day=date(2026, 11, 12), **kwargs)
    other = daily_slots(day=date(2026, 11, 13), **kwargs)
    assert [slot.at for slot in first] == [slot.at for slot in again]
    assert [slot.at for slot in first] != [slot.at for slot in other]


def test_daily_target_stays_inside_the_range() -> None:
    values = {
        daily_target(seed=1, day=date(2026, 11, day), minimum=3, maximum=6)
        for day in range(1, 29)
    }
    assert values <= {3, 4, 5, 6}
    assert min(values) >= 3 and max(values) <= 6
    assert len(values) > 1


def test_slot_due_only_fires_inside_the_tolerance() -> None:
    slots = daily_slots(
        day=date(2026, 11, 12),
        target_posts=3,
        window_start_hour=8,
        window_end_hour=23,
        min_gap_minutes=60,
        seed=3,
    )
    first = slots[0]
    assert slot_due(slots, first.at, tolerance_minutes=25) is first
    assert slot_due(slots, first.at + timedelta(minutes=10), tolerance_minutes=25) is first
    assert slot_due(slots, first.at + timedelta(minutes=90), tolerance_minutes=25) is None
    assert slot_due(slots, first.at - timedelta(minutes=10), tolerance_minutes=25) is None


# --- ramp ---------------------------------------------------------------
def test_warmup_account_gets_the_conservative_floor() -> None:
    allowance = compute_allowance(
        settings=settings(), account_created_at=NOW - timedelta(days=2), now=NOW
    )
    assert allowance.stage == "warming"
    assert allowance.posts_per_day == 3
    assert allowance.replies_per_day == 10
    assert allowance.follows_per_day == 10
    assert allowance.threads_allowed is False
    assert allowance.link_share <= 0.2


def test_established_account_gets_full_volume() -> None:
    allowance = compute_allowance(
        settings=settings(), account_created_at=NOW - timedelta(days=60), now=NOW
    )
    assert allowance.stage == "established"
    assert allowance.posts_per_day == 6
    assert allowance.replies_per_day == 15
    assert allowance.follows_per_day == 40
    assert allowance.threads_allowed is True


def test_ramping_stage_sits_between_the_two_ends() -> None:
    early = compute_allowance(
        settings=settings(), account_created_at=NOW - timedelta(days=16), now=NOW
    )
    late = compute_allowance(
        settings=settings(), account_created_at=NOW - timedelta(days=26), now=NOW
    )
    assert early.stage == "ramping" and late.stage == "ramping"
    assert early.follows_per_day < late.follows_per_day
    assert 10 <= early.follows_per_day <= late.follows_per_day <= 40


def test_unknown_age_is_treated_as_new() -> None:
    allowance = compute_allowance(settings=settings(), account_created_at=None, now=NOW)
    assert allowance.stage == "warming"
    assert allowance.posts_per_day == 3


# --- originality --------------------------------------------------------
HEADLINE = "All 19 Indian seafarers on board a Liberia-flagged tanker are safe after an attack"
SOURCE_BODY = (
    "The shipping minister said all crew members have been evacuated safely and the "
    "vessel is now heading to the nearest port for inspection by authorities."
)


def test_headline_repost_is_rejected() -> None:
    report = assess_originality(
        text=HEADLINE, settings=settings(), source_headline=HEADLINE
    )
    assert report.passed is False
    assert any("headline" in issue for issue in report.issues)


def test_own_take_with_context_passes() -> None:
    report = assess_originality(
        text=(
            "Another tanker hit near the UAE coast, and the crew walked away. "
            "That is the third this month on the same route, which says more about "
            "naval cover than about luck.\n\nThe shipping ministry confirmed the evacuation."
        ),
        settings=settings(),
        source_headline=HEADLINE,
        source_excerpt=SOURCE_BODY,
    )
    assert report.passed is True, report.issues
    assert report.commentary_words >= 6


def test_long_verbatim_passage_is_rejected() -> None:
    report = assess_originality(
        text=(
            "Worth watching: " + SOURCE_BODY + " More as it develops."
        ),
        settings=settings(),
        source_headline=HEADLINE,
        source_excerpt=SOURCE_BODY,
    )
    assert report.passed is False
    assert any("verbatim" in issue for issue in report.issues)


def test_operator_drafts_bypass_the_headline_rule() -> None:
    report = assess_originality(
        text=HEADLINE, settings=settings(), source_headline=HEADLINE, is_operator=True
    )
    assert report.passed is True


# --- coordination guard -------------------------------------------------
def _follow(account: int, handle: str, minutes: int) -> GrowthAction:
    return GrowthAction(
        kind="follow",
        at=NOW + timedelta(minutes=minutes),
        account_id=account,
        target_handle=handle,
    )


def test_cross_account_target_is_rejected() -> None:
    report = review_actions(
        planned=[_follow(2, "someone", 0)],
        history=[_follow(1, "someone", -60)],
        settings=settings(),
        now=NOW,
    )
    assert report.allowed == []
    assert report.rejected[0].rule == "cross_account_target"


def test_same_kind_spacing_is_enforced() -> None:
    report = review_actions(
        planned=[_follow(1, "a", 0), _follow(1, "b", 1)],
        settings=settings(growth_follow_min_seconds=90),
        now=NOW,
    )
    assert [action.target_handle for action in report.allowed] == ["a"]
    assert report.rejected[0].rule == "kind_spacing"


def test_follow_then_like_burst_is_rejected() -> None:
    follow = _follow(1, "target", 0)
    like = GrowthAction(
        kind="like",
        at=NOW + timedelta(minutes=1),
        account_id=1,
        target_handle="target",
    )
    report = review_actions(
        planned=[like],
        history=[follow],
        # Raise the daily cap so the burst rule is what rejects the like.
        settings=settings(growth_max_actions_per_target_per_day=2),
        now=NOW,
    )
    assert report.allowed == []
    assert [item.rule for item in report.rejected] == ["follow_like_burst"]


def test_per_target_daily_cap_is_enforced() -> None:
    history = [
        GrowthAction(
            kind="reply",
            at=NOW - timedelta(hours=1),
            account_id=1,
            target_handle="chatty",
        )
    ]
    planned = [
        GrowthAction(
            kind="like",
            at=NOW + timedelta(minutes=30),
            account_id=1,
            target_handle="chatty",
        )
    ]
    report = review_actions(
        planned=planned, history=history, settings=settings(), now=NOW
    )
    assert report.allowed == []
    assert report.rejected[0].rule == "target_daily_cap"


# --- planner ------------------------------------------------------------
def test_plan_respects_age_cap_and_mid_size_band() -> None:
    plan = build_plan(
        settings=settings(posts_per_day_min=5, posts_per_day_max=5),
        account_id=1,
        account_created_at=NOW - timedelta(days=3),
        now=NOW,
        reply_candidates=[
            ReplyTarget(handle="mid", followers=5_000, score=2.0),
            ReplyTarget(handle="mega", followers=5_000_000, score=3.0),
            ReplyTarget(handle="nano", followers=12, score=1.0),
        ],
        follow_candidates=[f"acct{i}" for i in range(60)],
    )
    assert plan.stage == "warming"
    assert plan.target_posts == 3  # warm-up cap wins over the requested 5
    assert [target.handle for target in plan.reply_targets] == ["mid"]
    assert any("mid-size ceiling" in note for note in plan.notes)
    assert len(plan.follow_targets) == 10  # warm-up follow allowance
    assert plan.link_budget <= plan.target_posts


def test_plan_uses_local_timezone_for_the_day() -> None:
    plan = build_plan(
        settings=settings(timezone="Asia/Kolkata"),
        account_id=1,
        account_created_at=NOW - timedelta(days=90),
        now=datetime(2026, 11, 12, 20, 0, tzinfo=UTC),
    )
    assert plan.day == datetime(2026, 11, 13, 1, 30, tzinfo=ZoneInfo("Asia/Kolkata")).date()
    assert plan.stage == "established"


@pytest.mark.parametrize("age_days,expected_max", [(1, 3), (20, 6), (90, 6)])
def test_plan_never_exceeds_the_policy_cap(age_days: int, expected_max: int) -> None:
    plan = build_plan(
        settings=settings(posts_per_day_min=6, posts_per_day_max=6),
        account_id=1,
        account_created_at=NOW - timedelta(days=age_days),
        now=NOW,
    )
    assert plan.target_posts <= expected_max
