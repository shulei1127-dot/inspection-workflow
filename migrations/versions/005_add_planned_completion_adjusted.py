"""add_planned_completion_adjusted

Revision ID: 005
Revises: 004
Create Date: 2026-06-01 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('work_orders', sa.Column(
        'planned_completion_adjusted',
        sa.Boolean,
        nullable=False,
        server_default=sa.text('false'),
    ))


def downgrade():
    op.drop_column('work_orders', 'planned_completion_adjusted')