"""add review_audit_logs table

Revision ID: 005
Revises: 004
Create Date: 2026-06-30 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'review_audit_logs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('project_id', sa.String(64), nullable=False, index=True),
        sa.Column('project_name', sa.String(255), nullable=True),
        sa.Column('customer_name', sa.String(255), nullable=True),
        sa.Column('conclusion', sa.String(32), nullable=False),
        sa.Column('region', sa.String(64), nullable=True),
        sa.Column('delivery_type', sa.String(64), nullable=True),
        sa.Column('project_type', sa.String(64), nullable=True),
        sa.Column('rules_result', JSONB, nullable=True),
        sa.Column('dingtalk_writeback', JSONB, nullable=True),
        sa.Column('pts_review_writeback', JSONB, nullable=True),
        sa.Column('trigger_source', sa.String(32), nullable=False, server_default='manual'),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table('review_audit_logs')
