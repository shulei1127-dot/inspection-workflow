"""AITable snapshot model for change detection.

Stores field value snapshots of AITable records so that
subsequent polls can detect field-level changes.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class AITableSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "aitable_snapshots"

    source_table: Mapped[str] = mapped_column(String(32), nullable=False)
    # source_table values: "daily_service" (日常增值服务进展) or "dispatch" (客户巡检派单)

    record_id: Mapped[str] = mapped_column(String(128), nullable=False)

    field_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # field_values example: {"engineer": "userId1", "supplier": "平云", "email_sent": "否"}

    __table_args__ = (
        UniqueConstraint("source_table", "record_id", name="uq_snapshot_source_record"),
        Index("ix_snapshot_source_table", "source_table"),
    )
