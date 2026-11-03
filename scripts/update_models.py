import re

with open("app/memory/models.py", "r", encoding="utf-8") as f:
    text = f.read()

models_to_add = """

# ===========================================================================
# Phase 3: Human Feedback Loop & Editorial Learning Models
# ===========================================================================

FEEDBACK_APPROVED = "approved"
FEEDBACK_REJECTED = "rejected"
FEEDBACK_EDITED = "edited"
FEEDBACK_ACTIONS = (FEEDBACK_APPROVED, FEEDBACK_REJECTED, FEEDBACK_EDITED)

REJECTION_REASONS = (
    "factual_error",
    "weak_hook",
    "generic_ai_writing",
    "poor_angle",
    "duplicate_coverage",
    "insufficient_research",
    "incorrect_tone",
    "poor_structure",
    "uninteresting_topic",
    "other",
)

REC_STATUS_PROPOSED = "proposed"
REC_STATUS_APPROVED = "approved"
REC_STATUS_REJECTED = "rejected"
REC_STATUS_APPLIED = "applied"
REC_STATUSES = (REC_STATUS_PROPOSED, REC_STATUS_APPROVED, REC_STATUS_REJECTED, REC_STATUS_APPLIED)


class EditorialFeedback(Base, TimestampMixin):
    __tablename__ = "editorial_feedback"
    __table_args__ = (
        Index("ix_editorial_feedback_account_draft", "account_id", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("editorial_evaluations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    candidate_angle: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_critic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verification_status_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    operator_identity: Mapped[str] = mapped_column(
        String(128), default="admin", nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="feedback_records")


class EditorialRecommendation(Base, TimestampMixin):
    __tablename__ = "editorial_recommendations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pattern_type: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_feedback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    suggested_dna_adjustment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(32), default=REC_STATUS_PROPOSED, nullable=False, index=True
    )


class PersonaVersionHistory(Base, TimestampMixin):
    __tablename__ = "persona_version_history"
    __table_args__ = (
        Index("ix_persona_version_history_version", "account_id", "version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    style_guide: Mapped[str] = mapped_column(Text, default="", nullable=False)
    voice_guidelines: Mapped[str] = mapped_column(Text, default="", nullable=False)
    banned_phrases: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    change_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("editorial_recommendations.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[str] = mapped_column(String(128), default="admin", nullable=False)
"""

# Normalize line endings to find target
nl = "\r\n" if "\r\n" in text else "\n"
target = f"    claims: Mapped[list[EvidenceClaim]] = relationship({nl}        back_populates=\"draft\", cascade=\"all, delete-orphan\"{nl}    )"
replacement = f"    claims: Mapped[list[EvidenceClaim]] = relationship({nl}        back_populates=\"draft\", cascade=\"all, delete-orphan\"{nl}    ){nl}    feedback_records: Mapped[list[EditorialFeedback]] = relationship({nl}        back_populates=\"draft\", cascade=\"all, delete-orphan\"{nl}    )"

if target in text:
    text = text.replace(target, replacement, 1)
    text = text + models_to_add
    with open("app/memory/models.py", "w", encoding="utf-8") as f:
        f.write(text)
    print("SUCCESS")
else:
    print("Target not found directly, checking regex")
    pattern = r'(claims:\s*Mapped\[list\[EvidenceClaim\]\]\s*=\s*relationship\([^)]+\))'
    m = re.search(pattern, text)
    if m:
        text = text[:m.end()] + f"{nl}    feedback_records: Mapped[list[EditorialFeedback]] = relationship({nl}        back_populates=\"draft\", cascade=\"all, delete-orphan\"{nl}    )" + text[m.end():]
        text = text + models_to_add
        with open("app/memory/models.py", "w", encoding="utf-8") as f:
            f.write(text)
        print("SUCCESS VIA REGEX")
    else:
        print("FAILED TO MATCH")
