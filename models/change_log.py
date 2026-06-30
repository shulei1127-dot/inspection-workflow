"""ChangeLog model — daily change knowledge-base entries."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChangeLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "change_logs"

    change_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # git_commit / manual
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # bug_fix / feature_opt / important_change / other
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_hash: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    author: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    extra_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pushed_to_dingtalk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
