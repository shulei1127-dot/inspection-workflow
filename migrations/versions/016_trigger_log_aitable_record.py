"""Add AITable record identity to trigger logs."""

from alembic import op
import sqlalchemy as sa


revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trigger_logs",
        sa.Column("aitable_record_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_trigger_logs_aitable_record_id",
        "trigger_logs",
        ["aitable_record_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_trigger_logs_aitable_record_id", table_name="trigger_logs")
    op.drop_column("trigger_logs", "aitable_record_id")
