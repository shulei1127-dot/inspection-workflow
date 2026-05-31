"""add summaries column to email_pre_analysis

Revision ID: 003
Revises: 002
Create Date: 2026-05-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_pre_analysis",
        sa.Column("summaries", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_pre_analysis", "summaries")
