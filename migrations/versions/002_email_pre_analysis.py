"""add email_pre_analysis table

Revision ID: 002
Revises: 001
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_pre_analysis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aitable_record_id", sa.String(128), unique=True, nullable=False),
        sa.Column("analysis_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("inspection_date", sa.String(64), nullable=True),
        sa.Column("quantity", sa.String(64), nullable=True),
        sa.Column("emails", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("ai_info", postgresql.JSONB, nullable=True),
        sa.Column("aitable_fields", postgresql.JSONB, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_email_pre_analysis_aitable_record_id", "email_pre_analysis", ["aitable_record_id"])
    op.create_index("ix_email_pre_analysis_analysis_status", "email_pre_analysis", ["analysis_status"])


def downgrade() -> None:
    op.drop_index("ix_email_pre_analysis_analysis_status")
    op.drop_index("ix_email_pre_analysis_aitable_record_id")
    op.drop_table("email_pre_analysis")