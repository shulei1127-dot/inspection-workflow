"""add_change_logs_table

Revision ID: 008
Revises: 007
Create Date: 2026-06-13 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'change_logs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('change_date', sa.Date(), nullable=False, index=True),
        sa.Column('source_type', sa.String(20), nullable=False),
        sa.Column('category', sa.String(32), nullable=False, index=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('git_hash', sa.String(40), nullable=True, unique=True),
        sa.Column('author', sa.String(100), nullable=False, server_default=''),
        sa.Column('extra_data', JSONB, nullable=True),
        sa.Column('pushed_to_dingtalk', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('pushed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('change_logs')
