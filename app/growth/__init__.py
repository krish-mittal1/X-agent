"""Account growth controls.

This package answers a single question for every outbound action: *should this
happen right now, in this shape, from this account?* Four deterministic pieces
combine into that answer:

* :mod:`app.growth.pacing`      - irregular, human-looking timing
* :mod:`app.growth.ramp`        - limits that follow account age
* :mod:`app.growth.commentary`  - "does this post add anything of our own?"
* :mod:`app.growth.alts`        - no cross-account / bursty coordination
* :mod:`app.growth.planner`     - the daily plan that ties them together

Nothing here talks to X. The planner produces a plan; the publisher, the reply
queue and the operator decide whether to act on it.
"""

from app.growth.alts import (
    CoordinationReport,
    GrowthAction,
    Rejection,
    review_actions,
)
from app.growth.commentary import OriginalityReport, assess_originality
from app.growth.pacing import (
    PostingSlot,
    daily_slots,
    daily_target,
    engagement_times,
    resolve_timezone,
    slot_due,
)
from app.growth.planner import DailyGrowthPlan, ReplyTarget, build_plan
from app.growth.ramp import GrowthAllowance, compute_allowance

__all__ = [
    "CoordinationReport",
    "DailyGrowthPlan",
    "GrowthAction",
    "GrowthAllowance",
    "OriginalityReport",
    "PostingSlot",
    "Rejection",
    "ReplyTarget",
    "assess_originality",
    "build_plan",
    "compute_allowance",
    "daily_slots",
    "daily_target",
    "engagement_times",
    "resolve_timezone",
    "review_actions",
    "slot_due",
]
