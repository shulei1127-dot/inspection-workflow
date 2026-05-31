"""add_dt_synced_month

Revision ID: 004
Revises: 003
Create Date: 2026-05-27 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('work_orders', sa.Column('dt_synced_month', sa.String(7), nullable=True, index=True))


def downgrade():
    op.drop_column('work_orders', 'dt_synced_month')