"""SQLAlchemy 2.x models: relational state plus pgvector-backed memory."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# The vector column dimension is fixed in the schema. EMBEDDING_DIMENSIONS is
# validated against this value at startup; changing it requires a migration and
# the re-embedding script (see scripts/reembed.py).
EMBEDDING_DIMENSIONS = 384

# Category constants for memory_records.category
MEMORY_PERSONA = "persona"
MEMORY_EPISODIC = "episodic"
MEMORY_SEMANTIC = "semantic"
MEMORY_CONVERSATION = "conversation"
MEMORY_CATEGORIES = (MEMORY_PERSONA, MEMORY_EPISODIC, MEMORY_SEMANTIC, MEMORY_CONVERSATION)

# Draft statuses
DRAFT_PENDING_VERIFICATION = "pending_verification"
DRAFT_NEEDS_REVIEW = "needs_review"
DRAFT_APPROVED = "approved"
DRAFT_READY = "ready"
DRAFT_REJECTED = "rejected"
DRAFT_BLOCKED = "blocked"
DRAFT_PUBLISHED = "published"
DRAFT_NEEDS_RECONCILIATION = "needs_reconciliation"
DRAFT_FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    x_user_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    personas: Mapped[list[AccountPersona]] = relationship(back_populates="account")


class AccountPersona(Base, TimestampMixin):
    __tablename__ = "account_personas"
    __table_args__ = (UniqueConstraint("account_id", "name", "version", name="uq_persona_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    style_guide: Mapped[str] = mapped_column(Text, nullable=False, default="")
    interests: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    boundaries: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    account: Mapped[Account] = relationship(back_populates="personas")


class SourceDocument(Base, TimestampMixin):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("account_id", "url_hash", name="uq_source_url"),
        Index("ix_source_documents_account_retrieved", "account_id", "retrieved_at"),
        Index("ix_source_documents_account_score", "account_id", "candidate_score"),
        Index("ix_source_documents_search_vector", "search_vector", postgresql_using="gin"),
        CheckConstraint(
            "source_type IN ('rss','github','web','x','manual')",
            name="ck_source_documents_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(256))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    fetch_status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    fetch_error: Mapped[str | None] = mapped_column(Text)
    candidate_score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))", persisted=True),
    )


class Draft(Base, TimestampMixin):
    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="uq_draft_idempotency"),
        Index("ix_drafts_account_status", "account_id", "status"),
        CheckConstraint("kind IN ('original','reply')", name="ck_drafts_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="original")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=DRAFT_PENDING_VERIFICATION)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    topic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thread_parts: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(String(64))

    conversation_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_threads.id", ondelete="SET NULL")
    )
    in_reply_to_post_id: Mapped[str | None] = mapped_column(String(32))
    target_author_id: Mapped[str | None] = mapped_column(String(32))
    target_author_handle: Mapped[str | None] = mapped_column(String(64))
    source_document_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    candidate_score: Mapped[float | None] = mapped_column(Float)

    verification_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    policy_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    claims: Mapped[list[EvidenceClaim]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    feedback_records: Mapped[list[EditorialFeedback]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )


class ConversationThread(Base, TimestampMixin):
    __tablename__ = "conversation_threads"
    __table_args__ = (
        UniqueConstraint("account_id", "x_conversation_id", name="uq_conversation_thread"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_conversation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    root_post_id: Mapped[str | None] = mapped_column(String(32))
    topic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("draft_id", "position", name="uq_post_draft_position"),
        Index("ix_posts_account_published", "account_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"))
    x_post_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="original")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[str | None] = mapped_column(String(32))
    in_reply_to_post_id: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Interaction(Base, TimestampMixin):
    __tablename__ = "interactions"
    __table_args__ = (UniqueConstraint("account_id", "x_post_id", name="uq_interaction_post"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_threads.id", ondelete="SET NULL")
    )
    x_post_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author_x_user_id: Mapped[str | None] = mapped_column(String(32))
    author_handle: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )


class EvidenceClaim(Base, TimestampMixin):
    __tablename__ = "evidence_claims"
    __table_args__ = (
        CheckConstraint(
            "claim_type IN ('fact','interpretation','estimate','opinion')",
            name="ck_evidence_claim_type",
        ),
        CheckConstraint(
            "status IN ('verified','unverified','contradicted','stale')",
            name="ck_evidence_claim_status",
        ),
        Index("ix_evidence_claims_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), index=True
    )
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(16), nullable=False, default="fact")
    supporting_quote: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unverified")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    notes: Mapped[str | None] = mapped_column(Text)

    draft: Mapped[Draft | None] = relationship(back_populates="claims")


class MemoryRecord(Base, TimestampMixin):
    __tablename__ = "memory_records"
    __table_args__ = (
        UniqueConstraint("account_id", "content_hash", name="uq_memory_content"),
        Index("ix_memory_account_category", "account_id", "category"),
        Index("ix_memory_account_conversation", "account_id", "conversation_thread_id"),
        Index("ix_memory_records_search_vector", "search_vector", postgresql_using="gin"),
        CheckConstraint(
            "category IN ('persona','episodic','semantic','conversation')",
            name="ck_memory_category",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    conversation_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_threads.id", ondelete="SET NULL")
    )
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(summary, ''))",
            persisted=True,
        ),
    )

    embeddings: Mapped[list[MemoryEmbedding]] = relationship(
        back_populates="memory_record", cascade="all, delete-orphan"
    )


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "memory_record_id", "embedding_model", "chunk_index", name="uq_memory_embedding_chunk"
        ),
        Index("ix_memory_embeddings_model", "embedding_model"),
        Index(
            "ix_memory_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    memory_record_id: Mapped[int] = mapped_column(
        ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    memory_record: Mapped[MemoryRecord] = relationship(back_populates="embeddings")


class PublishingJob(Base, TimestampMixin):
    __tablename__ = "publishing_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_publishing_job_idempotency"),
        Index("ix_publishing_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="original")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)


class ActionLog(Base):
    __tablename__ = "action_logs"
    __table_args__ = (Index("ix_action_logs_account_created", "account_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OptOutRecord(Base):
    __tablename__ = "opt_out_records"
    __table_args__ = (UniqueConstraint("account_id", "x_user_id", name="uq_opt_out_user"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    x_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    source_post_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelUsageRecord(Base):
    __tablename__ = "model_usage_records"
    __table_args__ = (Index("ix_model_usage_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False, default="generate")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_type: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PostMetric(Base):
    __tablename__ = "post_metrics"
    __table_args__ = (UniqueConstraint("post_id", "fetched_at", name="uq_post_metric_snapshot"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_job_run_key"),
        Index("ix_job_runs_job_started", "job_name", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    run_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


# ===========================================================================
# Multi-Source Research Engine Models (Feature 1)
# ===========================================================================

class ResearchReport(Base, TimestampMixin):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    executive_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    what_happened: Mapped[str] = mapped_column(Text, nullable=False, default="")
    key_entities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    verified_facts: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    unresolved_claims: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    conflicting_info: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    historical_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stakeholder_analysis: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    potential_implications: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    sources: Mapped[list[ResearchSource]] = relationship(
        "ResearchSource", back_populates="report", cascade="all, delete-orphan"
    )
    claims: Mapped[list[ResearchClaim]] = relationship(
        "ResearchClaim", back_populates="report", cascade="all, delete-orphan"
    )
    __allow_unmapped__ = True
    _loaded_sources: list[Any] | None = None


class ResearchSource(Base, TimestampMixin):
    __tablename__ = "research_sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("research_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    publisher: Mapped[str] = mapped_column(String(256), nullable=False, default="Unknown")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="news")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    content_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    syndication_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    report: Mapped[ResearchReport] = relationship("ResearchReport", back_populates="sources")


class ResearchClaim(Base, TimestampMixin):
    __tablename__ = "research_claims"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("research_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    context_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_disputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    report: Mapped[ResearchReport] = relationship("ResearchReport", back_populates="claims")


# ===========================================================================
# Event Intelligence & Timeline Memory Models (Feature 2)
# ===========================================================================

class Event(Base, TimestampMixin):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="geopolitics")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    initial_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    latest_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    developments: Mapped[list[EventDevelopment]] = relationship(
        "EventDevelopment", back_populates="event", cascade="all, delete-orphan", order_by="EventDevelopment.development_date"
    )
    entities: Mapped[list[EventEntity]] = relationship(
        "EventEntity", back_populates="event", cascade="all, delete-orphan"
    )
    post_links: Mapped[list[EventPostLink]] = relationship(
        "EventPostLink", back_populates="event", cascade="all, delete-orphan"
    )


class EventDevelopment(Base, TimestampMixin):
    __tablename__ = "event_developments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    development_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    significance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    delta_type: Mapped[str] = mapped_column(String(64), nullable=False, default="continuation")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    event: Mapped[Event] = relationship("Event", back_populates="developments")


class EventEntity(Base, TimestampMixin):
    __tablename__ = "event_entities"
    __table_args__ = (Index("ix_event_entities_name", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(128), nullable=False, default="participant")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    event: Mapped[Event] = relationship("Event", back_populates="entities")


class EventRelationship(Base, TimestampMixin):
    __tablename__ = "event_relationships"
    __table_args__ = (
        UniqueConstraint("source_event_id", "target_event_id", "relationship_type", name="uq_event_relationship"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class EventPostLink(Base, TimestampMixin):
    __tablename__ = "event_post_links"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context_angle: Mapped[str] = mapped_column(String(128), nullable=False, default="coverage")

    event: Mapped[Event] = relationship("Event", back_populates="post_links")


class KnowledgeMemory(Base, TimestampMixin):
    __tablename__ = "knowledge_memories"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="geopolitics")
    topic: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.9)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class EditorialMemory(Base, TimestampMixin):
    __tablename__ = "editorial_memories"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    angle: Mapped[str] = mapped_column(String(64), nullable=False)
    topic: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(64), nullable=False)
    hook_type: Mapped[str] = mapped_column(String(64), nullable=False, default="provocative_question")
    writing_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))


# ===========================================================================
# Human-Like Editorial Engine & Critic Models (Features 4 & 5)
# ===========================================================================

class EditorialEvaluation(Base, TimestampMixin):
    __tablename__ = "editorial_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    overall_score: Mapped[float] = mapped_column(Float, nullable=False, default=75.0)
    factual_score: Mapped[float] = mapped_column(Float, nullable=False, default=80.0)
    originality_score: Mapped[float] = mapped_column(Float, nullable=False, default=70.0)
    insight_score: Mapped[float] = mapped_column(Float, nullable=False, default=70.0)
    naturalness_score: Mapped[float] = mapped_column(Float, nullable=False, default=75.0)
    persona_score: Mapped[float] = mapped_column(Float, nullable=False, default=80.0)
    clarity_score: Mapped[float] = mapped_column(Float, nullable=False, default=85.0)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    critic_reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    suggestions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class EditorialRule(Base, TimestampMixin):
    __tablename__ = "editorial_rules"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EditorialExample(Base, TimestampMixin):
    __tablename__ = "editorial_examples"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    angle: Mapped[str] = mapped_column(String(64), nullable=False, default="analytical")
    post_text: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


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
