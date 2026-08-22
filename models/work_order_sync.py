"""Monthly AITable sync association for deferred work orders."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkOrderSync(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One PTS work order's AITable instance for a target sync month."""

    __tablename__ = "work_order_syncs"

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pts_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sync_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    aitable_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    create_eligible: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("pts_order_id", "sync_month", name="uq_work_order_sync_pts_month"),
        Index("ix_work_order_sync_pts_month_status", "pts_order_id", "sync_month", "sync_status"),
    )
