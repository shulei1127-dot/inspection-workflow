"""Read-only inspection report audit result."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReportAudit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "report_audits"

    aitable_record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pts_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attachment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    document_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    llm_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "aitable_record_id",
            "attachment_fingerprint",
            "rule_version",
            name="uq_report_audit_source_version",
        ),
        Index("ix_report_audits_customer_status", "customer_name", "status"),
    )
