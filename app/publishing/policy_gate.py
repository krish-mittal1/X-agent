"""Deterministic publication policy gates.

An LLM's claim that something is allowed is irrelevant here: every gate reads
application state (configuration, draft status, verification report, counts,
opt-out records) and returns a decision the caller must obey.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.memory.models import Draft
from app.publishing.text_rules import validate_text_rules
from app.schemas.publishing import PolicyCheck, PolicyDecision


def _check(name: str, passed: bool, detail: str, severity: str = "hard") -> PolicyCheck:
    return PolicyCheck(name=name, passed=passed, detail=detail, severity=severity)  # type: ignore[arg-type]


def _verification(draft: Draft) -> dict[str, Any]:
    return draft.verification_report or {}


def _duplicate_report(draft: Draft) -> dict[str, Any] | None:
    policy = draft.policy_report or {}
    duplicate = policy.get("duplicate")
    return duplicate if isinstance(duplicate, dict) else None


def _parts(draft: Draft) -> list[str]:
    return [draft.body, *[part for part in (draft.thread_parts or []) if part]]


def evaluate_original_post(
    *,
    settings: Settings,
    draft: Draft,
    account_active: bool,
    posts_today: int,
    last_published_at: datetime | None,
    now: datetime | None = None,
    trending_topic: bool = False,
    already_published: bool = False,
) -> PolicyDecision:
    reference = now or datetime.now(UTC)
    checks: list[PolicyCheck] = []
    checks.append(_check(
        "draft_kind_is_original", draft.kind == "original",
        f"draft kind is {draft.kind!r}",
    ))
    checks.append(_check(
        "emergency_pause", not settings.emergency_pause,
        "emergency pause is active; all outbound actions are halted", "soft",
    ))
    checks.append(_check(
        "publishing_enabled", settings.publishing_enabled,
        "publishing is disabled by configuration", "soft",
    ))
    checks.append(_check("account_active", account_active, "account is inactive"))
    checks.append(_check(
        "draft_ready", draft.status in ("ready", "approved"),
        f"draft status is {draft.status!r}; only verified drafts may publish",
    ))
    checks.append(_check(
        "not_already_published", not already_published,
        "draft already has published posts",
    ))

    verification = _verification(draft)
    is_operator = bool(
        (draft.policy_report or {}).get("source") == "operator_command"
        or (draft.policy_report or {}).get("operator_approved")
        or (draft.policy_report or {}).get("operator_override")
        or verification.get("operator_approved")
        or verification.get("operator_command")
    )
    passed_verification = bool(verification.get("passed")) or is_operator
    checks.append(_check(
        "verification_passed", passed_verification,
        "draft verification did not pass",
    ))
    if settings.require_verified_claims and not is_operator:
        unverified = int(verification.get("unverified_facts", 0) or 0)
        checks.append(_check(
            "no_unverified_facts", unverified == 0,
            f"{unverified} unverified factual claim(s)",
        ))

    duplicate = _duplicate_report(draft)
    if duplicate is None and not is_operator:
        checks.append(_check(
            "duplicate_check_present", False, "no duplicate-check record on the draft"
        ))
    else:
        is_dup = bool(duplicate.get("duplicate")) if duplicate else False
        checks.append(_check(
            "not_duplicate", not is_dup,
            "draft is a duplicate/near-duplicate of a recent post",
        ))

    text_rules = validate_text_rules(settings, _parts(draft), format=draft.tone)
    checks.append(_check(
        "text_rules", text_rules.passed, "; ".join(text_rules.issues) or "text rules pass"
    ))
    checks.append(_check(
        "has_sources", bool(draft.source_document_ids), "draft has no research sources"
    ))
    if draft.candidate_score is not None:
        checks.append(_check(
            "candidate_quality",
            draft.candidate_score >= settings.publish_min_score,
            f"candidate score {draft.candidate_score:.2f} is below the publishing floor "
            f"{settings.publish_min_score:.2f}",
        ))
    if not settings.allow_trending_topics:
        checks.append(_check(
            "not_trending_topic", not trending_topic,
            "automated publishing about a trending topic is prohibited",
        ))
    checks.append(_check(
        "daily_post_budget", posts_today < settings.max_original_posts_per_day,
        f"{posts_today} original posts in the last 24h; limit "
        f"{settings.max_original_posts_per_day}",
        "soft",
    ))
    if last_published_at is not None and not is_operator:
        elapsed = reference - last_published_at
        checks.append(_check(
            "min_spacing", elapsed >= timedelta(hours=settings.min_hours_between_posts),
            f"only {elapsed.total_seconds() / 3600:.2f}h since last post; "
            f"minimum is {settings.min_hours_between_posts}h",
            "soft",
        ))
    has_credentials = (
        settings.dry_run
        or settings.x_write_credentials_present
        or (settings.x_publishing_mode == "brave_cdp" and bool(settings.x_auth_token and settings.x_ct0))
        or (settings.x_publishing_mode == "local_draft" and (settings.dry_run or not settings.publishing_enabled))
    )
    checks.append(_check(
        "x_credentials",
        has_credentials,
        "X write credentials are missing (dry-run would be allowed)",
    ))
    return PolicyDecision.from_checks(checks)


def evaluate_reply(
    *,
    settings: Settings,
    draft: Draft,
    account_active: bool,
    replies_today: int,
    has_inbound_interaction: bool,
    conversation_has_context: bool,
    opt_out: bool,
    already_published: bool = False,
    now: datetime | None = None,
) -> PolicyDecision:
    """Reply-sending gate.

    Public AI replies require prior written approval from X. Both the approval
    flag and the sending switch must be enabled by an operator; a reviewer
    approval alone is never sufficient.
    """
    checks: list[PolicyCheck] = []
    checks.append(_check(
        "ai_reply_approval", settings.ai_reply_approval_obtained,
        "prior written approval from X for automated replies has not been recorded",
        "soft",
    ))
    checks.append(_check(
        "ai_reply_sending_enabled", settings.ai_reply_publishing_enabled,
        "AI reply sending is disabled by configuration",
        "soft",
    ))
    checks.append(_check(
        "draft_kind_is_reply", draft.kind == "reply", f"draft kind is {draft.kind!r}"
    ))
    checks.append(_check(
        "human_approved", draft.status == "approved",
        "reply draft has not been approved by a reviewer",
    ))
    checks.append(_check(
        "has_target_post", bool(draft.in_reply_to_post_id), "reply has no target post id"
    ))
    checks.append(_check(
        "inbound_intent", has_inbound_interaction,
        "no inbound interaction from this account; keyword-only replies are prohibited",
    ))
    checks.append(_check(
        "conversation_context", conversation_has_context,
        "insufficient conversation context to reply meaningfully",
    ))
    checks.append(_check("opt_out_respected", not opt_out, "user has opted out of automated replies"))
    checks.append(_check("account_active", account_active, "account is inactive"))
    checks.append(_check("not_already_published", not already_published, "reply already published"))

    verification = _verification(draft)
    checks.append(_check(
        "verification_passed", bool(verification.get("passed")) or not settings.require_verified_claims,
        "reply verification did not pass",
    ))
    duplicate = _duplicate_report(draft)
    checks.append(_check(
        "not_duplicate", not (duplicate or {}).get("duplicate"),
        "reply duplicates a previous post",
    ))
    text_rules = validate_text_rules(settings, _parts(draft), format=draft.tone)
    checks.append(_check(
        "text_rules", text_rules.passed, "; ".join(text_rules.issues) or "text rules pass"
    ))
    checks.append(_check(
        "reply_budget", replies_today < settings.max_replies_per_day,
        f"{replies_today} replies in the last 24h; limit {settings.max_replies_per_day}",
        "soft",
    ))
    has_credentials = (
        settings.dry_run
        or settings.x_write_credentials_present
        or (settings.x_publishing_mode == "brave_cdp" and bool(settings.x_auth_token and settings.x_ct0))
        or (settings.x_publishing_mode == "local_draft" and (settings.dry_run or not settings.publishing_enabled))
    )
    checks.append(_check(
        "x_credentials",
        has_credentials,
        "X write credentials are missing",
        "soft",
    ))
    return PolicyDecision.from_checks(checks)
