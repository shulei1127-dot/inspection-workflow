"""Add crm_project_id to work_orders

Revision ID: 012_crm_project_id
Revises: 011_visit_log
"""
from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_orders", sa.Column("crm_project_id", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("work_orders", "crm_project_id")
