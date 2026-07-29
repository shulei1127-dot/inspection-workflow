"""Add explicit AITable create eligibility to work orders."""

from alembic import op
import sqlalchemy as sa


revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_orders",
        sa.Column(
            "dt_create_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_work_orders_dt_create_eligible",
        "work_orders",
        ["dt_create_eligible"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_orders_dt_create_eligible", table_name="work_orders")
    op.drop_column("work_orders", "dt_create_eligible")
