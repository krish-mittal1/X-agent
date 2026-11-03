"""Editorial Learning Engine: Translates human feedback patterns into explainable DNA recommendations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.memory.models import (
    FEEDBACK_EDITED,
    FEEDBACK_REJECTED,
    REC_STATUS_APPLIED,
    REC_STATUS_APPROVED,
    REC_STATUS_PROPOSED,
    AccountPersona,
    EditorialFeedback,
    EditorialRecommendation,
    PersonaVersionHistory,
)


class EditorialLearner:
    """Analyzes human feedback to propose safe, explainable Persona adjustments."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze_and_generate_recommendations(
        self, account_id: int, min_feedback_threshold: int = 5
    ) -> list[EditorialRecommendation]:
        """Scan feedback patterns and propose new recommendations if evidence supports them."""
        # 1. Fetch recent feedback records
        stmt = (
            select(EditorialFeedback)
            .where(EditorialFeedback.account_id == account_id)
            .order_by(EditorialFeedback.created_at.desc())
        )
        res = await self.session.execute(stmt)
        feedbacks = list(res.scalars().all())

        if len(feedbacks) < min_feedback_threshold:
            return []

        existing_recs_res = await self.session.execute(
            select(EditorialRecommendation.pattern_type)
            .where(
                EditorialRecommendation.account_id == account_id,
                EditorialRecommendation.status.in_([REC_STATUS_PROPOSED, REC_STATUS_APPROVED]),
            )
        )
        existing_pattern_types = set(existing_recs_res.scalars().all())

        new_recommendations: list[EditorialRecommendation] = []

        # Pattern A: Excessive 'generic_ai_writing' or 'weak_hook' rejections
        rejection_reasons = [fb.rejection_reason for fb in feedbacks if fb.action == FEEDBACK_REJECTED]
        generic_ai_count = rejection_reasons.count("generic_ai_writing")
        weak_hook_count = rejection_reasons.count("weak_hook")

        if generic_ai_count >= 3 and "anti_cliche_directive" not in existing_pattern_types:
            rec = EditorialRecommendation(
                account_id=account_id,
                pattern_type="anti_cliche_directive",
                evidence_summary=f"Operator rejected {generic_ai_count} drafts due to generic AI writing style.",
                supporting_feedback_count=generic_ai_count,
                sample_size=len(feedbacks),
                confidence=round(min(1.0, generic_ai_count / 5.0), 2),
                suggested_dna_adjustment={
                    "voice_guidelines_addition": "Adopt a punchy, fact-first voice. Ban breathless meta-commentary.",
                    "banned_phrases_addition": [
                        "delve",
                        "game-changer",
                        "testament to",
                        "in an era where",
                        "it remains to be seen",
                    ],
                },
                status=REC_STATUS_PROPOSED,
            )
            self.session.add(rec)
            new_recommendations.append(rec)

        if weak_hook_count >= 3 and "hook_directness_directive" not in existing_pattern_types:
            rec = EditorialRecommendation(
                account_id=account_id,
                pattern_type="hook_directness_directive",
                evidence_summary=f"Operator rejected {weak_hook_count} drafts due to weak opening hooks.",
                supporting_feedback_count=weak_hook_count,
                sample_size=len(feedbacks),
                confidence=round(min(1.0, weak_hook_count / 5.0), 2),
                suggested_dna_adjustment={
                    "voice_guidelines_addition": "Lead with the single most surprising verified fact or counter-intuitive stat. Never open with vague rhetorical questions.",
                },
                status=REC_STATUS_PROPOSED,
            )
            self.session.add(rec)
            new_recommendations.append(rec)

        # Pattern B: Systematic text shortening in human edits
        edited_records = [fb for fb in feedbacks if fb.action == FEEDBACK_EDITED and fb.edited_text]
        shortened_count = 0
        for fb in edited_records:
            if fb.original_text_snapshot and len(fb.edited_text or "") < len(fb.original_text_snapshot) * 0.8:
                shortened_count += 1

        if shortened_count >= 2 and "conciseness_enforcement" not in existing_pattern_types:
            rec = EditorialRecommendation(
                account_id=account_id,
                pattern_type="conciseness_enforcement",
                evidence_summary=f"Operator shortened {shortened_count} drafts by more than 20% in manual edits.",
                supporting_feedback_count=shortened_count,
                sample_size=len(edited_records),
                confidence=round(min(1.0, shortened_count / 3.0), 2),
                suggested_dna_adjustment={
                    "style_guide_addition": "Target concise 180-220 characters for single tweets. Strip filler adjectives.",
                },
                status=REC_STATUS_PROPOSED,
            )
            self.session.add(rec)
            new_recommendations.append(rec)

        if new_recommendations:
            await self.session.commit()
            for r in new_recommendations:
                await self.session.refresh(r)

        return new_recommendations

    async def apply_recommendation(
        self, recommendation_id: int, approved_by: str = "admin"
    ) -> AccountPersona:
        """Apply an approved recommendation to the AccountPersona and save version history."""
        rec = await self.session.get(EditorialRecommendation, recommendation_id)
        if rec is None:
            raise ValueError(f"Recommendation {recommendation_id} not found")

        persona_res = await self.session.execute(
            select(AccountPersona)
            .where(AccountPersona.account_id == rec.account_id, AccountPersona.is_active == True)  # noqa: E712
            .order_by(AccountPersona.version.desc())
        )
        persona = persona_res.scalars().first()
        if persona is None:
            raise ValueError(f"Active persona not found for account {rec.account_id}")

        # Current version snapshot
        boundaries_list = list(persona.boundaries or [])
        style_guide_text = persona.style_guide or ""

        # Increment version
        new_version = persona.version + 1

        # Save previous state in history
        history = PersonaVersionHistory(
            account_id=rec.account_id,
            version=persona.version,
            system_prompt=persona.system_prompt,
            style_guide=style_guide_text,
            voice_guidelines=style_guide_text,
            banned_phrases=boundaries_list,
            change_description=f"Prior version before applying recommendation #{rec.id} ({rec.pattern_type})",
            recommendation_id=rec.id,
            approved_by=approved_by,
        )
        self.session.add(history)

        # Apply adjustments
        adj = rec.suggested_dna_adjustment or {}
        if "voice_guidelines_addition" in adj:
            persona.style_guide = f"{persona.style_guide}\n\n[Guideline] {adj['voice_guidelines_addition']}".strip()
        if "style_guide_addition" in adj:
            persona.style_guide = f"{persona.style_guide}\n\n[Style] {adj['style_guide_addition']}".strip()
        if "banned_phrases_addition" in adj:
            for phrase in adj["banned_phrases_addition"]:
                if phrase not in boundaries_list:
                    boundaries_list.append(phrase)
            persona.boundaries = boundaries_list
            flag_modified(persona, "boundaries")

        persona.version = new_version
        rec.status = REC_STATUS_APPLIED

        await self.session.commit()
        await self.session.refresh(persona)
        await self.session.refresh(rec)
        return persona

    async def rollback_persona(
        self, account_id: int, target_version: int, approved_by: str = "admin"
    ) -> AccountPersona:
        """Roll back persona to a previously snapshotted version in PersonaVersionHistory."""
        # Find snapshot
        stmt = select(PersonaVersionHistory).where(
            PersonaVersionHistory.account_id == account_id,
            PersonaVersionHistory.version == target_version,
        )
        res = await self.session.execute(stmt)
        snapshot = res.scalars().first()
        if snapshot is None:
            raise ValueError(f"Persona version history {target_version} not found for account {account_id}")

        persona_res = await self.session.execute(
            select(AccountPersona)
            .where(AccountPersona.account_id == account_id, AccountPersona.is_active == True)  # noqa: E712
            .order_by(AccountPersona.version.desc())
        )
        persona = persona_res.scalars().first()
        if persona is None:
            raise ValueError(f"Active persona not found for account {account_id}")

        # Record current before rolling back
        current_history = PersonaVersionHistory(
            account_id=account_id,
            version=persona.version,
            system_prompt=persona.system_prompt,
            style_guide=persona.style_guide or "",
            voice_guidelines=persona.style_guide or "",
            banned_phrases=list(persona.boundaries or []),
            change_description=f"Snapshot before rollback to version {target_version}",
            approved_by=approved_by,
        )
        self.session.add(current_history)

        # Restore
        persona.system_prompt = snapshot.system_prompt
        persona.style_guide = snapshot.style_guide
        persona.boundaries = list(snapshot.banned_phrases or [])
        flag_modified(persona, "boundaries")
        persona.version = persona.version + 1  # Increment version on rollback for monotonicity

        await self.session.commit()
        await self.session.refresh(persona)
        return persona
