"""Add sales confirm push log table."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_confirm_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("crm_project_url", sa.Text(), nullable=True),
        sa.Column("sales_user_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="sent"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("need_create_inspection", sa.String(length=32), nullable=True),
        sa.Column("need_contact_value_added", sa.String(length=32), nullable=True),
        sa.Column("no_create_reason", sa.Text(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sales_confirm_logs_record_id", "sales_confirm_logs", ["record_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_sales_confirm_logs_record_id", table_name="sales_confirm_logs")
    op.drop_table("sales_confirm_logs")
