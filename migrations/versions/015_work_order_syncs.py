"""Add monthly AITable sync associations for deferred work orders."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_order_syncs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pts_order_id", sa.String(length=64), nullable=False),
        sa.Column("sync_month", sa.String(length=7), nullable=False),
        sa.Column("aitable_record_id", sa.String(length=128), nullable=True),
        sa.Column("sync_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("create_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pts_order_id", "sync_month", name="uq_work_order_sync_pts_month"),
        sa.UniqueConstraint("aitable_record_id", name="uq_work_order_sync_aitable_record"),
    )
    op.create_index("ix_work_order_syncs_work_order_id", "work_order_syncs", ["work_order_id"])
    op.create_index("ix_work_order_syncs_pts_order_id", "work_order_syncs", ["pts_order_id"])
    op.create_index("ix_work_order_syncs_sync_month", "work_order_syncs", ["sync_month"])
    op.create_index("ix_work_order_syncs_sync_status", "work_order_syncs", ["sync_status"])
    op.create_index("ix_work_order_syncs_create_eligible", "work_order_syncs", ["create_eligible"])
    op.create_index(
        "ix_work_order_sync_pts_month_status",
        "work_order_syncs",
        ["pts_order_id", "sync_month", "sync_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_order_sync_pts_month_status", table_name="work_order_syncs")
    op.drop_index("ix_work_order_syncs_create_eligible", table_name="work_order_syncs")
    op.drop_index("ix_work_order_syncs_sync_status", table_name="work_order_syncs")
    op.drop_index("ix_work_order_syncs_sync_month", table_name="work_order_syncs")
    op.drop_index("ix_work_order_syncs_pts_order_id", table_name="work_order_syncs")
    op.drop_index("ix_work_order_syncs_work_order_id", table_name="work_order_syncs")
    op.drop_table("work_order_syncs")
