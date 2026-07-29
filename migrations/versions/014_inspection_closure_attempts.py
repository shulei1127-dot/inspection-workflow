"""Add durable inspection closure V2 attempt state."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspection_closure_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aitable_record_id", sa.String(length=128), nullable=False),
        sa.Column("pts_order_id", sa.String(length=64), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("coordinator_status", sa.String(length=32), nullable=False, server_default="ELIGIBLE"),
        sa.Column("attachment_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stage_before", sa.String(length=128), nullable=True),
        sa.Column("stage_after", sa.String(length=128), nullable=True),
        sa.Column("creator_id", sa.String(length=128), nullable=True),
        sa.Column("creator_name", sa.String(length=255), nullable=True),
        sa.Column("claim_by_id", sa.String(length=128), nullable=True),
        sa.Column("claim_by_name", sa.String(length=255), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_notification_key", sa.String(length=128), nullable=True),
        sa.Column("manual_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_notification_status", sa.String(length=32), nullable=True),
        sa.Column("aitable_writeback_status", sa.String(length=32), nullable=True),
        sa.Column("report_writeback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closure_writeback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "aitable_record_id",
            "pts_order_id",
            "report_fingerprint",
            name="uq_inspection_closure_attempt_identity",
        ),
    )
    op.create_index(
        "ix_inspection_closure_attempt_status",
        "inspection_closure_attempts",
        ["coordinator_status"],
    )
    op.create_index(
        "ix_inspection_closure_attempt_status_retry",
        "inspection_closure_attempts",
        ["coordinator_status", "next_retry_at"],
    )
    op.create_index(
        "ix_inspection_closure_attempt_record",
        "inspection_closure_attempts",
        ["aitable_record_id"],
    )
    op.create_index(
        "ix_inspection_closure_attempt_pts",
        "inspection_closure_attempts",
        ["pts_order_id"],
    )
    op.create_index(
        "ix_inspection_closure_attempt_writeback",
        "inspection_closure_attempts",
        ["aitable_writeback_status"],
    )
    op.create_index(
        "ix_inspection_closure_attempt_manual_key",
        "inspection_closure_attempts",
        ["manual_notification_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_inspection_closure_attempt_manual_key", table_name="inspection_closure_attempts")
    op.drop_index("ix_inspection_closure_attempt_writeback", table_name="inspection_closure_attempts")
    op.drop_index("ix_inspection_closure_attempt_pts", table_name="inspection_closure_attempts")
    op.drop_index("ix_inspection_closure_attempt_record", table_name="inspection_closure_attempts")
    op.drop_index("ix_inspection_closure_attempt_status_retry", table_name="inspection_closure_attempts")
    op.drop_index("ix_inspection_closure_attempt_status", table_name="inspection_closure_attempts")
    op.drop_table("inspection_closure_attempts")
