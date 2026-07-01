"""add visit_logs table

Revision ID: 011
Revises: 010
Create Date: 2026-07-01 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'visit_logs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', sa.String(64), nullable=False, index=True),
        sa.Column('review_log_id', UUID(as_uuid=True), nullable=True),
        sa.Column('pts_visit_id', sa.String(64), nullable=True, index=True),
        sa.Column('project_name', sa.String(255), nullable=True),
        sa.Column('customer_name', sa.String(255), nullable=True),
        sa.Column('region', sa.String(64), nullable=True),
        sa.Column('execution_mode', sa.String(32), nullable=False, server_default='direct_http'),
        sa.Column('step_fetch_delivery', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_create_visit', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_find_visit', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_fill_feedback', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_finish_visit', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_post_check', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('step_dingtalk_writeback', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending', index=True),
        sa.Column('company_id', sa.String(64), nullable=True),
        sa.Column('visitor_id', sa.String(64), nullable=True),
        sa.Column('contact_id', sa.String(64), nullable=True),
        sa.Column('product_id', sa.String(64), nullable=True),
        sa.Column('form_id', sa.String(64), nullable=True),
        sa.Column('content_id', sa.String(64), nullable=True),
        sa.Column('visit_url', sa.Text, nullable=True),
        sa.Column('satisfaction_score', sa.Integer, nullable=False, server_default='5'),
        sa.Column('visit_note', sa.Text, nullable=True),
        sa.Column('dingtalk_record_id', sa.String(128), nullable=True),
        sa.Column('dingtalk_writeback', JSONB, nullable=True),
        sa.Column('current_step', sa.String(64), nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('retry_count', sa.Integer, nullable=False, server_default='0'),
        sa.Column('step_log', JSONB, nullable=True),
        sa.Column('trigger_source', sa.String(32), nullable=False, server_default='auto'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table('visit_logs')
