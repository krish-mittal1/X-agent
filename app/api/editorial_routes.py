"""API endpoints for Editorial Engine, Critic Evaluations, Human Feedback, and Learning."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.agent.editorial_learner import EditorialLearner
from app.analytics.feedback_analytics import FeedbackAnalyticsService
from app.analytics.performance import PerformanceAnalyticsService, PerformanceReport
from app.api.deps import AdminAuth, DbSession, SettingsDep
from app.api.drafts import resolve_account
from app.memory.models import (
    DRAFT_READY,
    DRAFT_REJECTED,
    FEEDBACK_ACTIONS,
    FEEDBACK_APPROVED,
    FEEDBACK_EDITED,
    FEEDBACK_REJECTED,
    REC_STATUS_APPLIED,
    REC_STATUS_REJECTED,
    REJECTION_REASONS,
    Draft,
    EditorialEvaluation,
    EditorialFeedback,
    EditorialRecommendation,
    ResearchReport,
    ResearchSource,
    SourceDocument,
)
from app.memory.repositories import PostRepository
from app.publishing.text_rules import find_duplicate, normalize_typography, validate_text_rules
from app.research.verification import (
    _FINANCIAL_OR_NUMERICAL,
    ClaimVerifier,
    normalize_for_match,
)
from app.schemas.content import DraftClaim

router = APIRouter(prefix="/api/editorial", tags=["editorial"])
analytics_router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class SwitchAngleRequest(BaseModel):
    draft_id: int
    target_angle: str = Field(
        description="'straight_news', 'context_explainer', 'engaging_angle', or a stored alternative"
    )


class EditorialDNAUpdateRequest(BaseModel):
    voice_guidelines: str | None = None
    banned_phrases: list[str] | None = None
    change_description: str | None = None


class FeedbackSubmissionRequest(BaseModel):
    draft_id: int
    candidate_angle: str = Field(default="single_tweet")
    candidate_version: int = Field(default=1)
    action: str = Field(description="'approved', 'rejected', or 'edited'")
    rejection_reason: str | None = None
    operator_comment: str | None = None
    edited_text: str | None = None


class DraftEditRequest(BaseModel):
    edited_text: str
    operator_comment: str | None = None
    candidate_angle: str | None = None


class RecommendationReviewRequest(BaseModel):
    action: str = Field(description="'approve' or 'reject'")


class RollbackRequest(BaseModel):
    target_version: int


@router.get("/evaluations/{draft_id}")
async def get_draft_evaluation(
    draft_id: int,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Retrieve the independent critic evaluation and alternative angles for a draft."""
    account = await resolve_account(session, settings.x_persona_handle)
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    eval_query = (
        select(EditorialEvaluation)
        .where(EditorialEvaluation.draft_id == draft_id)
        .order_by(EditorialEvaluation.id.desc())
    )
    res = await session.execute(eval_query)
    evaluation = res.scalars().first()

    policy = draft.policy_report or {}
    alternatives = policy.get("alternative_angles", [])

    eval_data = None
    if evaluation is not None:
        eval_data = {
            "overall_score": evaluation.overall_score,
            "factual_score": evaluation.factual_score,
            "originality_score": evaluation.originality_score,
            "insight_score": evaluation.insight_score,
            "naturalness_score": evaluation.naturalness_score,
            "persona_score": evaluation.persona_score,
            "clarity_score": evaluation.clarity_score,
            "verdict": evaluation.verdict,
            "critic_reasoning": evaluation.critic_reasoning,
            "flags": evaluation.flags,
            "suggestions": evaluation.suggestions,
        }

    return {
        "draft_id": draft_id,
        "current_angle": draft.tone or "straight_news",
        "current_body": draft.body,
        "evaluation": eval_data,
        "alternative_angles": alternatives,
    }


@router.post("/switch-angle")
async def switch_draft_angle(
    req: SwitchAngleRequest,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Safely switch an existing draft's text to one of its validated alternative angles."""
    account = await resolve_account(session, settings.x_persona_handle)
    draft = await session.get(Draft, req.draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    policy = draft.policy_report or {}
    alternatives: list[dict[str, Any]] = policy.get("alternative_angles", [])

    match = next((alt for alt in alternatives if alt.get("angle") == req.target_angle), None)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail=f"Target angle '{req.target_angle}' not found in alternative angles",
        )

    # 1. Normalize typography and extract parts
    parts: list[str] = match.get("parts", [])
    if not parts:
        hook = match.get("hook", "")
        parts = [hook] if hook else [draft.body]
    parts = [normalize_typography(part) for part in parts if part.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="Target angle has empty body content")

    # 2. Text rule validation
    text_rules = validate_text_rules(settings, parts, format=draft.tone or "single_tweet")
    if not text_rules.passed:
        raise HTTPException(
            status_code=400,
            detail=f"Target angle violates text rules: {'; '.join(text_rules.issues)}",
        )

    # 3. Duplicate check against account's recent posts
    recent_posts = await PostRepository.recent(session, account.id, limit=20)
    joined_text = "\n\n".join(parts)
    duplicate = find_duplicate(
        text=joined_text,
        recent_texts=[p.text for p in recent_posts],
        settings=settings,
        embedding_similarity=None,
    )
    if duplicate.duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"Target angle is a duplicate of recent coverage: {'; '.join(duplicate.reasons)}",
        )

    # 4. Multi-Source Claim Verification for target angle
    evidence_sources: list[Any] = []
    if draft.source_document_ids:
        docs = (
            await session.scalars(
                select(SourceDocument).where(SourceDocument.id.in_(draft.source_document_ids))
            )
        ).all()
        evidence_sources.extend(docs)

    conflicting_info: list[dict[str, Any]] = []
    report_id = policy.get("research_report_id")
    if report_id:
        rep = await session.get(ResearchReport, report_id)
        if rep is not None:
            conflicting_info = rep.conflicting_info or []
            existing_urls = {getattr(d, "url", "") for d in evidence_sources}
            rep_sources = (
                await session.scalars(
                    select(ResearchSource).where(ResearchSource.report_id == report_id)
                )
            ).all()
            for s in rep_sources:
                if s.url and s.url not in existing_urls:
                    evidence_sources.append(s)
                    existing_urls.add(s.url)

    raw_claims = match.get("claims", [])
    claims_to_verify: list[DraftClaim] = []
    for c in raw_claims:
        if isinstance(c, dict):
            claims_to_verify.append(DraftClaim(**c))
        elif isinstance(c, DraftClaim):
            claims_to_verify.append(c)

    verifier = ClaimVerifier(settings)
    verification = verifier.verify(
        claims_to_verify,
        evidence_sources,
        now=datetime.now(UTC),
        conflicting_claims=conflicting_info,
    )

    if not verification.passed or verification.hard_stop_triggered:
        reasons = "; ".join(verification.hard_stop_reasons or verification.issues)
        raise HTTPException(
            status_code=400,
            detail=f"Target angle failed claim verification: {reasons}",
        )

    # 5. Swap angle safely
    old_angle_entry = {
        "angle": draft.tone or "straight_news",
        "hook": draft.body.split("\n")[0] if draft.body else "",
        "parts": [draft.body] + (draft.thread_parts or []),
        "rationale": draft.rationale or "",
        "claims": (draft.verification_report or {}).get("claims", []),
    }
    updated_alts = [alt for alt in alternatives if alt.get("angle") != req.target_angle]
    updated_alts.append(old_angle_entry)

    draft.tone = req.target_angle
    draft.body = parts[0]
    draft.thread_parts = parts[1:] if len(parts) > 1 else []
    if match.get("rationale"):
        draft.rationale = match.get("rationale")

    policy["alternative_angles"] = updated_alts
    policy["text_rules"] = text_rules.model_dump(mode="json")
    policy["duplicate"] = duplicate.model_dump(mode="json")
    draft.policy_report = dict(policy)
    draft.verification_report = verification.model_dump(mode="json")
    flag_modified(draft, "policy_report")
    flag_modified(draft, "verification_report")

    await session.commit()
    await session.refresh(draft)

    return {
        "success": True,
        "draft_id": draft.id,
        "new_angle": draft.tone,
        "new_body": draft.body,
        "thread_parts": draft.thread_parts,
        "verification_passed": verification.passed,
    }


# ===========================================================================
# Phase 3: Feedback, In-Place Editing, Learning Engine Endpoints
# ===========================================================================


@router.post("/feedback")
async def record_feedback(
    req: FeedbackSubmissionRequest,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Record an operator review decision (approve, reject, or edit feedback)."""
    if req.action not in FEEDBACK_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{req.action}'. Allowed: {', '.join(FEEDBACK_ACTIONS)}",
        )

    if req.action == FEEDBACK_REJECTED and (
        not req.rejection_reason or req.rejection_reason not in REJECTION_REASONS
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid or missing rejection_reason. Allowed: {', '.join(REJECTION_REASONS)}",
        )

    account = await resolve_account(session, settings.x_persona_handle)
    draft = await session.get(Draft, req.draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Fetch latest evaluation if available
    eval_query = (
        select(EditorialEvaluation)
        .where(EditorialEvaluation.draft_id == draft.id)
        .order_by(EditorialEvaluation.id.desc())
    )
    res = await session.execute(eval_query)
    evaluation = res.scalars().first()

    # Create feedback entry
    feedback = EditorialFeedback(
        account_id=account.id,
        draft_id=draft.id,
        evaluation_id=evaluation.id if evaluation else None,
        candidate_angle=req.candidate_angle or draft.tone or "single_tweet",
        candidate_version=req.candidate_version,
        action=req.action,
        rejection_reason=req.rejection_reason,
        operator_comment=req.operator_comment,
        original_text_snapshot=draft.body,
        edited_text=req.edited_text,
        original_critic_score=evaluation.overall_score if evaluation else None,
        verification_status_snapshot=draft.verification_report or {},
        operator_identity="admin",
    )
    session.add(feedback)

    # Update draft status based on action
    if req.action == FEEDBACK_APPROVED:
        # Approval means accepted by operator for local staging/review ONLY.
        # NEVER triggers X publishing.
        draft.status = DRAFT_READY
        draft.reviewed_by = "admin"
        draft.reviewed_at = datetime.now(UTC)
        draft.review_notes = f"Operator Approved: {req.operator_comment or 'Ready for staging'}"
    elif req.action == FEEDBACK_REJECTED:
        draft.status = DRAFT_REJECTED
        draft.reviewed_by = "admin"
        draft.reviewed_at = datetime.now(UTC)
        draft.review_notes = f"Operator Rejected [{req.rejection_reason}]: {req.operator_comment or ''}"

    await session.commit()
    await session.refresh(feedback)
    await session.refresh(draft)

    return {
        "success": True,
        "feedback_id": feedback.id,
        "draft_id": draft.id,
        "action": feedback.action,
        "draft_status": draft.status,
    }


@router.post("/feedback/{draft_id}/edit")
async def edit_draft(
    draft_id: int,
    req: DraftEditRequest,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Safely edit a draft in place. Validates typography, text rules, duplicates, and claims."""
    account = await resolve_account(session, settings.x_persona_handle)
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    new_text = normalize_typography(req.edited_text.strip())
    if not new_text:
        raise HTTPException(status_code=400, detail="Edited text cannot be empty")

    parts = [new_text]

    # 1. Text Rule Validation
    text_rules = validate_text_rules(settings, parts, format=draft.tone or "single_tweet")
    if not text_rules.passed:
        raise HTTPException(
            status_code=400,
            detail=f"Edited text violates text rules: {'; '.join(text_rules.issues)}",
        )

    # 2. Duplicate Detection
    recent_posts = await PostRepository.recent(session, account.id, limit=20)
    duplicate = find_duplicate(
        text=new_text,
        recent_texts=[p.text for p in recent_posts],
        settings=settings,
        embedding_similarity=None,
    )
    if duplicate.duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"Edited text is a duplicate of recent coverage: {'; '.join(duplicate.reasons)}",
        )

    # 3. Retrieve Evidence Sources for Claim Verification
    evidence_sources: list[Any] = []
    if draft.source_document_ids:
        docs = (
            await session.scalars(
                select(SourceDocument).where(SourceDocument.id.in_(draft.source_document_ids))
            )
        ).all()
        evidence_sources.extend(docs)

    policy = draft.policy_report or {}
    report_id = policy.get("research_report_id")
    conflicting_info: list[dict[str, Any]] = []
    if report_id:
        rep = await session.get(ResearchReport, report_id)
        if rep is not None:
            conflicting_info = rep.conflicting_info or []
            existing_urls = {getattr(d, "url", "") for d in evidence_sources}
            rep_sources = (
                await session.scalars(
                    select(ResearchSource).where(ResearchSource.report_id == report_id)
                )
            ).all()
            for s in rep_sources:
                if s.url and s.url not in existing_urls:
                    evidence_sources.append(s)
                    existing_urls.add(s.url)

    # Quantitative claim check on newly edited text
    new_numbers = _FINANCIAL_OR_NUMERICAL.findall(new_text)
    if new_numbers:
        # Check that these figures appear in at least one evidence source
        all_evidence_text = " ".join(
            [getattr(s, "content", "") or getattr(s, "excerpt", "") for s in evidence_sources]
        )
        norm_evidence = normalize_for_match(all_evidence_text)
        unverified_nums = []
        for num in new_numbers:
            clean_num = normalize_for_match(num.strip())
            if clean_num and clean_num not in norm_evidence:
                unverified_nums.append(num.strip())
        if unverified_nums:
            raise HTTPException(
                status_code=400,
                detail=f"Edited text contains unverified quantitative assertions not found in evidence sources: {', '.join(unverified_nums)}",
            )

    # Re-verify original claims that remain relevant
    existing_claims = (draft.verification_report or {}).get("claims", [])
    claims_to_verify = []
    for c in existing_claims:
        if isinstance(c, dict):
            claims_to_verify.append(DraftClaim(**c))
        elif isinstance(c, DraftClaim):
            claims_to_verify.append(c)

    verifier = ClaimVerifier(settings)
    verification = verifier.verify(
        claims_to_verify,
        evidence_sources,
        now=datetime.now(UTC),
        conflicting_claims=conflicting_info,
    )

    if not verification.passed or verification.hard_stop_triggered:
        reasons = "; ".join(verification.hard_stop_reasons or verification.issues)
        raise HTTPException(
            status_code=400,
            detail=f"Edited draft failed claim verification: {reasons}",
        )

    # 4. Save Version & Feedback
    original_snapshot = draft.body
    draft.body = new_text

    # Update policy report
    policy["is_operator_edited"] = True
    policy["text_rules"] = text_rules.model_dump(mode="json")
    policy["duplicate"] = duplicate.model_dump(mode="json")
    draft.policy_report = dict(policy)
    draft.verification_report = verification.model_dump(mode="json")
    flag_modified(draft, "policy_report")
    flag_modified(draft, "verification_report")

    # Record feedback entry
    eval_query = (
        select(EditorialEvaluation)
        .where(EditorialEvaluation.draft_id == draft.id)
        .order_by(EditorialEvaluation.id.desc())
    )
    res = await session.execute(eval_query)
    evaluation = res.scalars().first()

    feedback = EditorialFeedback(
        account_id=account.id,
        draft_id=draft.id,
        evaluation_id=evaluation.id if evaluation else None,
        candidate_angle=req.candidate_angle or draft.tone or "single_tweet",
        candidate_version=2,
        action=FEEDBACK_EDITED,
        rejection_reason=None,
        operator_comment=req.operator_comment or "Operator in-place edit",
        original_text_snapshot=original_snapshot,
        edited_text=new_text,
        original_critic_score=evaluation.overall_score if evaluation else None,
        verification_status_snapshot=draft.verification_report,
        operator_identity="admin",
    )
    session.add(feedback)

    await session.commit()
    await session.refresh(draft)
    await session.refresh(feedback)

    return {
        "success": True,
        "draft_id": draft.id,
        "new_body": draft.body,
        "is_operator_edited": True,
        "feedback_id": feedback.id,
        "verification_passed": verification.passed,
    }


@router.get("/feedback/{draft_id}")
async def get_draft_feedback_history(
    draft_id: int,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Retrieve all historical feedback records for a draft."""
    account = await resolve_account(session, settings.x_persona_handle)
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.account_id != account.id:
        raise HTTPException(status_code=404, detail="Draft not found")

    stmt = (
        select(EditorialFeedback)
        .where(EditorialFeedback.draft_id == draft_id)
        .order_by(EditorialFeedback.created_at.desc())
    )
    res = await session.execute(stmt)
    records = res.scalars().all()

    return {
        "draft_id": draft_id,
        "history": [
            {
                "id": f.id,
                "action": f.action,
                "rejection_reason": f.rejection_reason,
                "operator_comment": f.operator_comment,
                "original_text": f.original_text_snapshot,
                "edited_text": f.edited_text,
                "candidate_angle": f.candidate_angle,
                "original_critic_score": f.original_critic_score,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in records
        ],
    }


@router.get("/feedback/summary")
async def get_feedback_summary(
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
    min_threshold: int = 10,
) -> dict[str, Any]:
    """Retrieve human feedback analytics with cohort breakdowns and insufficient data flag."""
    account = await resolve_account(session, settings.x_persona_handle)
    service = FeedbackAnalyticsService(session, min_sample_threshold=min_threshold)
    return await service.get_summary(account.id)


@router.get("/learning/recommendations")
async def list_learning_recommendations(
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """List proposed and applied editorial learning recommendations."""
    account = await resolve_account(session, settings.x_persona_handle)
    learner = EditorialLearner(session)
    # Check for new patterns based on feedback
    await learner.analyze_and_generate_recommendations(account.id)

    stmt = (
        select(EditorialRecommendation)
        .where(EditorialRecommendation.account_id == account.id)
        .order_by(EditorialRecommendation.created_at.desc())
    )
    res = await session.execute(stmt)
    recs = res.scalars().all()

    return {
        "recommendations": [
            {
                "id": r.id,
                "pattern_type": r.pattern_type,
                "evidence_summary": r.evidence_summary,
                "supporting_feedback_count": r.supporting_feedback_count,
                "sample_size": r.sample_size,
                "confidence": r.confidence,
                "suggested_dna_adjustment": r.suggested_dna_adjustment,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recs
        ]
    }


@router.post("/learning/recommendations/{rec_id}/review")
async def review_recommendation(
    rec_id: int,
    req: RecommendationReviewRequest,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Approve or reject a proposed editorial learning recommendation."""
    account = await resolve_account(session, settings.x_persona_handle)
    rec = await session.get(EditorialRecommendation, rec_id)
    if rec is None or rec.account_id != account.id:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    learner = EditorialLearner(session)
    if req.action == "approve":
        persona = await learner.apply_recommendation(rec_id, approved_by="admin")
        return {
            "success": True,
            "status": REC_STATUS_APPLIED,
            "new_persona_version": persona.version,
            "message": "Recommendation successfully approved and applied to persona DNA.",
        }
    elif req.action == "reject":
        rec.status = REC_STATUS_REJECTED
        await session.commit()
        return {
            "success": True,
            "status": REC_STATUS_REJECTED,
            "message": "Recommendation rejected by operator.",
        }
    else:
        raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'.")


@router.post("/dna/rollback")
async def rollback_persona_dna(
    req: RollbackRequest,
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Roll back Persona Editorial DNA to a previously snapshotted version."""
    account = await resolve_account(session, settings.x_persona_handle)
    learner = EditorialLearner(session)
    try:
        persona = await learner.rollback_persona(
            account.id, req.target_version, approved_by="admin"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "success": True,
        "restored_version": req.target_version,
        "new_version": persona.version,
        "message": f"Successfully rolled back to version {req.target_version}.",
    }


@analytics_router.get("/performance/insights")
async def get_performance_insights(
    _: AdminAuth,
    session: DbSession,
    settings: SettingsDep,
    days: int = 30,
) -> PerformanceReport:
    """Retrieve post engagement intelligence and performance learning recommendations."""
    account = await resolve_account(session, settings.x_persona_handle)
    service = PerformanceAnalyticsService(session)
    return await service.generate_report(account_id=account.id, days=days)
