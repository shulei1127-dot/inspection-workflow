"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "work_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pts_order_id", sa.String(64), unique=True, nullable=False),
        sa.Column("pts_order_url", sa.Text, nullable=True),
        sa.Column("order_type", sa.String(128), nullable=True),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("engineer", sa.String(255), nullable=True),
        sa.Column("after_sale", sa.String(128), nullable=True),
        sa.Column("assigner_name", sa.String(128), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("partner_supplier", sa.String(255), nullable=True),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("contact_phone", sa.String(64), nullable=True),
        sa.Column("sales_name", sa.String(64), nullable=True),
        sa.Column("planned_completion", sa.Date, nullable=True),
        sa.Column("status", sa.String(64), nullable=True),
        sa.Column("email_sent", sa.String(16), nullable=False, server_default="否"),
        sa.Column("raw_data", postgresql.JSONB, nullable=True),
        sa.Column("dt_record_id", sa.String(128), nullable=True),
        sa.Column("dt_sync_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("dt_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_status", sa.String(32), nullable=False, server_default="待派单"),
        sa.Column("email_trigger_status", sa.String(32), nullable=False, server_default="待发送"),
        sa.Column("closure_status", sa.String(32), nullable=False, server_default="未闭环"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_work_orders_pts_order_id", "work_orders", ["pts_order_id"])
    op.create_index("ix_work_orders_dt_sync_status", "work_orders", ["dt_sync_status"])
    op.create_index("ix_work_orders_dispatch_status", "work_orders", ["dispatch_status"])
    op.create_index("ix_work_orders_email_trigger_status", "work_orders", ["email_trigger_status"])
    op.create_index("ix_work_orders_closure_status", "work_orders", ["closure_status"])

    op.create_table(
        "sync_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trigger_source", sa.String(32), nullable=False),
        sa.Column("sync_month", sa.String(7), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("fetched_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "trigger_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_orders.id"), nullable=False),
        sa.Column("trigger_type", sa.String(64), nullable=False),
        sa.Column("trigger_reason", sa.Text, nullable=True),
        sa.Column("request_payload", postgresql.JSONB, nullable=True),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "aitable_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_table", sa.String(32), nullable=False),
        sa.Column("record_id", sa.String(128), nullable=False),
        sa.Column("field_values", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_table", "record_id", name="uq_snapshot_source_record"),
    )
    op.create_index("ix_snapshot_source_table", "aitable_snapshots", ["source_table"])


def downgrade() -> None:
    op.drop_table("aitable_snapshots")
    op.drop_table("trigger_logs")
    op.drop_table("sync_logs")
    op.drop_table("work_orders")
