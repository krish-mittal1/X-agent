"""initial schema

Revision ID: a022afe3214f
Revises: 
Create Date: 2026-10-28 19:11:00.187455
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a022afe3214f'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('accounts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('handle', sa.String(length=64), nullable=False),
    sa.Column('x_user_id', sa.String(length=32), nullable=True),
    sa.Column('display_name', sa.String(length=128), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('handle'),
    sa.UniqueConstraint('x_user_id')
    )
    op.create_table('job_runs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('job_name', sa.String(length=64), nullable=False),
    sa.Column('run_key', sa.String(length=160), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_key', name='uq_job_run_key')
    )
    op.create_index('ix_job_runs_job_started', 'job_runs', ['job_name', 'started_at'], unique=False)
    op.create_table('account_personas',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('system_prompt', sa.Text(), nullable=False),
    sa.Column('style_guide', sa.Text(), nullable=False),
    sa.Column('interests', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('boundaries', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'name', 'version', name='uq_persona_version')
    )
    op.create_index(op.f('ix_account_personas_account_id'), 'account_personas', ['account_id'], unique=False)
    op.create_table('action_logs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_action_logs_account_created', 'action_logs', ['account_id', 'created_at'], unique=False)
    op.create_table('conversation_threads',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('x_conversation_id', sa.String(length=32), nullable=False),
    sa.Column('root_post_id', sa.String(length=32), nullable=True),
    sa.Column('topic', sa.Text(), nullable=False),
    sa.Column('state', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'x_conversation_id', name='uq_conversation_thread')
    )
    op.create_index(op.f('ix_conversation_threads_account_id'), 'conversation_threads', ['account_id'], unique=False)
    op.create_table('model_usage_records',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('operation', sa.String(length=64), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=False),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_model_usage_created', 'model_usage_records', ['created_at'], unique=False)
    op.create_table('opt_out_records',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('x_user_id', sa.String(length=32), nullable=False),
    sa.Column('handle', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('source_post_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'x_user_id', name='uq_opt_out_user')
    )
    op.create_table('source_documents',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('source_type', sa.String(length=16), nullable=False),
    sa.Column('source_identifier', sa.String(length=512), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('url_hash', sa.String(length=64), nullable=False),
    sa.Column('canonical_url', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('author', sa.String(length=256), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=False),
    sa.Column('reliability_score', sa.Float(), nullable=False),
    sa.Column('fetch_status', sa.String(length=16), nullable=False),
    sa.Column('fetch_error', sa.Text(), nullable=True),
    sa.Column('candidate_score', sa.Float(), nullable=True),
    sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('scored_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))", persisted=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source_type IN ('rss','github','web','x','manual')", name='ck_source_documents_type'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'url_hash', name='uq_source_url')
    )
    op.create_index('ix_source_documents_account_retrieved', 'source_documents', ['account_id', 'retrieved_at'], unique=False)
    op.create_index('ix_source_documents_account_score', 'source_documents', ['account_id', 'candidate_score'], unique=False)
    op.create_table('drafts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('topic', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('thread_parts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('tone', sa.String(length=64), nullable=True),
    sa.Column('conversation_thread_id', sa.BigInteger(), nullable=True),
    sa.Column('in_reply_to_post_id', sa.String(length=32), nullable=True),
    sa.Column('target_author_id', sa.String(length=32), nullable=True),
    sa.Column('target_author_handle', sa.String(length=64), nullable=True),
    sa.Column('source_document_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('candidate_score', sa.Float(), nullable=True),
    sa.Column('verification_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('policy_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('review_notes', sa.Text(), nullable=True),
    sa.Column('reviewed_by', sa.String(length=128), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('original','reply')", name='ck_drafts_kind'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_thread_id'], ['conversation_threads.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'idempotency_key', name='uq_draft_idempotency')
    )
    op.create_index('ix_drafts_account_status', 'drafts', ['account_id', 'status'], unique=False)
    op.create_table('interactions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('conversation_thread_id', sa.BigInteger(), nullable=True),
    sa.Column('x_post_id', sa.String(length=32), nullable=False),
    sa.Column('author_x_user_id', sa.String(length=32), nullable=True),
    sa.Column('author_handle', sa.String(length=64), nullable=True),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_thread_id'], ['conversation_threads.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'x_post_id', name='uq_interaction_post')
    )
    op.create_index(op.f('ix_interactions_account_id'), 'interactions', ['account_id'], unique=False)
    op.create_table('evidence_claims',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('draft_id', sa.BigInteger(), nullable=True),
    sa.Column('source_document_id', sa.BigInteger(), nullable=True),
    sa.Column('claim_text', sa.Text(), nullable=False),
    sa.Column('claim_type', sa.String(length=16), nullable=False),
    sa.Column('supporting_quote', sa.Text(), nullable=True),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("claim_type IN ('fact','interpretation','estimate','opinion')", name='ck_evidence_claim_type'),
    sa.CheckConstraint("status IN ('verified','unverified','contradicted','stale')", name='ck_evidence_claim_status'),
    sa.ForeignKeyConstraint(['draft_id'], ['drafts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_document_id'], ['source_documents.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_evidence_claims_draft', 'evidence_claims', ['draft_id'], unique=False)
    op.create_index(op.f('ix_evidence_claims_draft_id'), 'evidence_claims', ['draft_id'], unique=False)
    op.create_table('posts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('draft_id', sa.BigInteger(), nullable=True),
    sa.Column('x_post_id', sa.String(length=32), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('conversation_id', sa.String(length=32), nullable=True),
    sa.Column('in_reply_to_post_id', sa.String(length=32), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['draft_id'], ['drafts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('draft_id', 'position', name='uq_post_draft_position'),
    sa.UniqueConstraint('x_post_id')
    )
    op.create_index('ix_posts_account_published', 'posts', ['account_id', 'published_at'], unique=False)
    op.create_table('publishing_jobs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('draft_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=160), nullable=False),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['draft_id'], ['drafts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key', name='uq_publishing_job_idempotency')
    )
    op.create_index('ix_publishing_jobs_status', 'publishing_jobs', ['status'], unique=False)
    op.create_table('memory_records',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('account_id', sa.BigInteger(), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('record_type', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('source_document_id', sa.BigInteger(), nullable=True),
    sa.Column('conversation_thread_id', sa.BigInteger(), nullable=True),
    sa.Column('post_id', sa.BigInteger(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(summary, ''))", persisted=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('persona','episodic','semantic','conversation')", name='ck_memory_category'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_thread_id'], ['conversation_threads.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_document_id'], ['source_documents.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'content_hash', name='uq_memory_content')
    )
    op.create_index('ix_memory_account_category', 'memory_records', ['account_id', 'category'], unique=False)
    op.create_index('ix_memory_account_conversation', 'memory_records', ['account_id', 'conversation_thread_id'], unique=False)
    op.create_table('post_metrics',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('post_id', sa.BigInteger(), nullable=False),
    sa.Column('impressions', sa.Integer(), nullable=False),
    sa.Column('likes', sa.Integer(), nullable=False),
    sa.Column('replies', sa.Integer(), nullable=False),
    sa.Column('reposts', sa.Integer(), nullable=False),
    sa.Column('quotes', sa.Integer(), nullable=False),
    sa.Column('bookmarks', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['post_id'], ['posts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('post_id', 'fetched_at', name='uq_post_metric_snapshot')
    )
    op.create_index(op.f('ix_post_metrics_post_id'), 'post_metrics', ['post_id'], unique=False)
    op.create_table('memory_embeddings',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('memory_record_id', sa.BigInteger(), nullable=False),
    sa.Column('embedding_model', sa.String(length=255), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('chunk_text', sa.Text(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['memory_record_id'], ['memory_records.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('memory_record_id', 'embedding_model', 'chunk_index', name='uq_memory_embedding_chunk')
    )
    op.create_index('ix_memory_embeddings_model', 'memory_embeddings', ['embedding_model'], unique=False)

    # Full-text indexes for hybrid retrieval (tsvector generated columns).
    op.execute(
        "CREATE INDEX ix_source_documents_search_vector ON source_documents "
        "USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_memory_records_search_vector ON memory_records "
        "USING gin (search_vector)"
    )
    # Approximate nearest-neighbour index for cosine similarity.
    op.execute(
        "CREATE INDEX ix_memory_embeddings_hnsw ON memory_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_embeddings_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_memory_records_search_vector")
    op.execute("DROP INDEX IF EXISTS ix_source_documents_search_vector")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_memory_embeddings_model', table_name='memory_embeddings')
    op.drop_table('memory_embeddings')
    op.drop_index(op.f('ix_post_metrics_post_id'), table_name='post_metrics')
    op.drop_table('post_metrics')
    op.drop_index('ix_memory_account_conversation', table_name='memory_records')
    op.drop_index('ix_memory_account_category', table_name='memory_records')
    op.drop_table('memory_records')
    op.drop_index('ix_publishing_jobs_status', table_name='publishing_jobs')
    op.drop_table('publishing_jobs')
    op.drop_index('ix_posts_account_published', table_name='posts')
    op.drop_table('posts')
    op.drop_index(op.f('ix_evidence_claims_draft_id'), table_name='evidence_claims')
    op.drop_index('ix_evidence_claims_draft', table_name='evidence_claims')
    op.drop_table('evidence_claims')
    op.drop_index(op.f('ix_interactions_account_id'), table_name='interactions')
    op.drop_table('interactions')
    op.drop_index('ix_drafts_account_status', table_name='drafts')
    op.drop_table('drafts')
    op.drop_index('ix_source_documents_account_score', table_name='source_documents')
    op.drop_index('ix_source_documents_account_retrieved', table_name='source_documents')
    op.drop_table('source_documents')
    op.drop_table('opt_out_records')
    op.drop_index('ix_model_usage_created', table_name='model_usage_records')
    op.drop_table('model_usage_records')
    op.drop_index(op.f('ix_conversation_threads_account_id'), table_name='conversation_threads')
    op.drop_table('conversation_threads')
    op.drop_index('ix_action_logs_account_created', table_name='action_logs')
    op.drop_table('action_logs')
    op.drop_index(op.f('ix_account_personas_account_id'), table_name='account_personas')
    op.drop_table('account_personas')
    op.drop_index('ix_job_runs_job_started', table_name='job_runs')
    op.drop_table('job_runs')
    op.drop_table('accounts')
    # ### end Alembic commands ###
