"""Add isolated inspection report audit table."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aitable_record_id", sa.String(length=128), nullable=False),
        sa.Column("pts_order_id", sa.String(length=64), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("product_name", sa.String(length=255), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("attachment_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("attachments", postgresql.JSONB(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("blocker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings", postgresql.JSONB(), nullable=True),
        sa.Column("document_meta", postgresql.JSONB(), nullable=True),
        sa.Column("llm_summary", sa.Text(), nullable=True),
        sa.Column("ai_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "aitable_record_id",
            "attachment_fingerprint",
            "rule_version",
            name="uq_report_audit_source_version",
        ),
    )
    op.create_index("ix_report_audits_aitable_record_id", "report_audits", ["aitable_record_id"])
    op.create_index("ix_report_audits_pts_order_id", "report_audits", ["pts_order_id"])
    op.create_index("ix_report_audits_customer_name", "report_audits", ["customer_name"])
    op.create_index("ix_report_audits_status", "report_audits", ["status"])
    op.create_index("ix_report_audits_customer_status", "report_audits", ["customer_name", "status"])


def downgrade() -> None:
    op.drop_index("ix_report_audits_customer_status", table_name="report_audits")
    op.drop_index("ix_report_audits_status", table_name="report_audits")
    op.drop_index("ix_report_audits_customer_name", table_name="report_audits")
    op.drop_index("ix_report_audits_pts_order_id", table_name="report_audits")
    op.drop_index("ix_report_audits_aitable_record_id", table_name="report_audits")
    op.drop_table("report_audits")
