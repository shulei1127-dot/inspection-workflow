"""交付转售后审核日志模型"""

import uuid

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class ReviewAuditLog(Base, TimestampMixin):
    __tablename__ = "review_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conclusion: Mapped[str] = mapped_column(String(32), nullable=False)  # 通过/不通过/转人工审核

    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    rules_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dingtalk_writeback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pts_review_writeback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    trigger_source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
