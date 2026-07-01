"""trigger_log: nullable work_order_id + ondelete cascade

Revision ID: 007
Revises: 006
Create Date: 2026-06-03 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    # Drop existing FK constraint first
    op.drop_constraint('trigger_logs_work_order_id_fkey', 'trigger_logs', type_='foreignkey')
    # Make work_order_id nullable
    op.alter_column('trigger_logs', 'work_order_id',
                    existing_type=sa.UUID(),
                    nullable=True)
    # Re-add FK with CASCADE delete
    op.create_foreign_key('trigger_logs_work_order_id_fkey', 'trigger_logs', 'work_orders',
                          ['work_order_id'], ['id'], ondelete='CASCADE')


def downgrade():
    op.drop_constraint('trigger_logs_work_order_id_fkey', 'trigger_logs', type_='foreignkey')
    op.alter_column('trigger_logs', 'work_order_id',
                    existing_type=sa.UUID(),
                    nullable=False)
    op.create_foreign_key('trigger_logs_work_order_id_fkey', 'trigger_logs', 'work_orders',
                          ['work_order_id'], ['id'])