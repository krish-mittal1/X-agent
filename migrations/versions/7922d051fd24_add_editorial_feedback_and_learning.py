"""add_editorial_feedback_and_learning

Revision ID: 7922d051fd24
Revises: f0168481b68d
Create Date: 2026-11-03 16:03:19.887168
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '7922d051fd24'
down_revision: str | None = 'f0168481b68d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. editorial_feedback
    op.create_table(
        'editorial_feedback',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('draft_id', sa.BigInteger(), nullable=False),
        sa.Column('evaluation_id', sa.BigInteger(), nullable=True),
        sa.Column('candidate_angle', sa.String(length=64), nullable=False),
        sa.Column('candidate_version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('rejection_reason', sa.String(length=64), nullable=True),
        sa.Column('operator_comment', sa.Text(), nullable=True),
        sa.Column('original_text_snapshot', sa.Text(), nullable=False),
        sa.Column('edited_text', sa.Text(), nullable=True),
        sa.Column('original_critic_score', sa.Float(), nullable=True),
        sa.Column('verification_status_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('operator_identity', sa.String(length=128), server_default='admin', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['draft_id'], ['drafts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evaluation_id'], ['editorial_evaluations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_editorial_feedback_account_id'), 'editorial_feedback', ['account_id'], unique=False)
    op.create_index(op.f('ix_editorial_feedback_draft_id'), 'editorial_feedback', ['draft_id'], unique=False)
    op.create_index(op.f('ix_editorial_feedback_evaluation_id'), 'editorial_feedback', ['evaluation_id'], unique=False)
    op.create_index(op.f('ix_editorial_feedback_action'), 'editorial_feedback', ['action'], unique=False)
    op.create_index(op.f('ix_editorial_feedback_created_at'), 'editorial_feedback', ['created_at'], unique=False)
    op.create_index('ix_editorial_feedback_account_draft', 'editorial_feedback', ['account_id', 'draft_id'], unique=False)

    # 2. editorial_recommendations
    op.create_table(
        'editorial_recommendations',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('pattern_type', sa.String(length=64), nullable=False),
        sa.Column('evidence_summary', sa.Text(), nullable=False),
        sa.Column('supporting_feedback_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('sample_size', sa.Integer(), server_default='0', nullable=False),
        sa.Column('confidence', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('suggested_dna_adjustment', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='proposed', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_editorial_recommendations_account_id'), 'editorial_recommendations', ['account_id'], unique=False)
    op.create_index(op.f('ix_editorial_recommendations_status'), 'editorial_recommendations', ['status'], unique=False)

    # 3. persona_version_history
    op.create_table(
        'persona_version_history',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=False),
        sa.Column('style_guide', sa.Text(), server_default='', nullable=False),
        sa.Column('voice_guidelines', sa.Text(), server_default='', nullable=False),
        sa.Column('banned_phrases', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('change_description', sa.Text(), server_default='', nullable=False),
        sa.Column('recommendation_id', sa.BigInteger(), nullable=True),
        sa.Column('approved_by', sa.String(length=128), server_default='admin', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recommendation_id'], ['editorial_recommendations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_persona_version_history_account_id'), 'persona_version_history', ['account_id'], unique=False)
    op.create_index(op.f('ix_persona_version_history_version'), 'persona_version_history', ['account_id', 'version'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_persona_version_history_version'), table_name='persona_version_history')
    op.drop_index(op.f('ix_persona_version_history_account_id'), table_name='persona_version_history')
    op.drop_table('persona_version_history')

    op.drop_index(op.f('ix_editorial_recommendations_status'), table_name='editorial_recommendations')
    op.drop_index(op.f('ix_editorial_recommendations_account_id'), table_name='editorial_recommendations')
    op.drop_table('editorial_recommendations')

    op.drop_index('ix_editorial_feedback_account_draft', table_name='editorial_feedback')
    op.drop_index(op.f('ix_editorial_feedback_created_at'), table_name='editorial_feedback')
    op.drop_index(op.f('ix_editorial_feedback_action'), table_name='editorial_feedback')
    op.drop_index(op.f('ix_editorial_feedback_evaluation_id'), table_name='editorial_feedback')
    op.drop_index(op.f('ix_editorial_feedback_draft_id'), table_name='editorial_feedback')
    op.drop_index(op.f('ix_editorial_feedback_account_id'), table_name='editorial_feedback')
    op.drop_table('editorial_feedback')
