"""Email pre-analysis model: stores AI-extracted info for email-pending AITable records.

Pre-analysis runs as a separate scheduled task, persists AI results in DB.
At send time, we re-download PDF (for attachment) and do a lightweight AITable
field refresh (emails, sales), but skip the heavy AI extraction step.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EmailPreAnalysis(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "email_pre_analysis"

    # AITable record ID (unique — one analysis per record)
    aitable_record_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True,
    )
    # Analysis lifecycle: pending → success / failed
    analysis_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True,
    )

    # ── AI extracted structured fields ──
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inspection_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    emails: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Raw AI response for debugging / re-processing
    ai_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Multi-product summaries: [{product, summary}, ...]
    summaries: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # AITable lightweight fields (refreshed at send time, not at analysis time)
    aitable_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )