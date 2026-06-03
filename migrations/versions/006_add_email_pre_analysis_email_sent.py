"""add email_pre_analysis email_sent column

Revision ID: 006
Revises: 005
Create Date: 2026-06-03 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('email_pre_analysis', sa.Column(
        'email_sent',
        sa.Boolean,
        nullable=False,
        server_default=sa.text('false'),
    ))


def downgrade():
    op.drop_column('email_pre_analysis', 'email_sent')