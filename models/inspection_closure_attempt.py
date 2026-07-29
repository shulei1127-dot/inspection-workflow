"""Durable state for idempotent inspection closure V2 attempts."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class InspectionClosureAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "inspection_closure_attempts"
    __table_args__ = (
        UniqueConstraint(
            "aitable_record_id",
            "pts_order_id",
            "report_fingerprint",
            name="uq_inspection_closure_attempt_identity",
        ),
        Index("ix_inspection_closure_attempt_status_retry", "coordinator_status", "next_retry_at"),
        Index("ix_inspection_closure_attempt_record", "aitable_record_id"),
        Index("ix_inspection_closure_attempt_pts", "pts_order_id"),
        Index("ix_inspection_closure_attempt_writeback", "aitable_writeback_status"),
    )

    aitable_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pts_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    report_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)

    coordinator_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ELIGIBLE", index=True)
    attachment_results: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    stage_before: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stage_after: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creator_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_by_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    manual_notification_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    manual_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_notification_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    aitable_writeback_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    report_writeback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closure_writeback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
