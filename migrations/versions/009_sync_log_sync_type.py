"""add sync_type column to sync_logs

Revision ID: 009
Revises: 008
Create Date: 2026-06-29 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'sync_logs',
        sa.Column('sync_type', sa.String(32), nullable=False, server_default='full_sync'),
    )


def downgrade():
    op.drop_column('sync_logs', 'sync_type')
