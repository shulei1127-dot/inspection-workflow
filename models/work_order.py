from datetime import date, datetime

from sqlalchemy import Date, String, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class WorkOrder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "work_orders"

    pts_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    pts_order_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engineer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    after_sale: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assigner_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partner_supplier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sales_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planned_completion: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_sent: Mapped[str] = mapped_column(String(16), default="否", nullable=False)

    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # DingTalk sync tracking
    dt_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dt_sync_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    dt_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dt_synced_month: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)  # Format: YYYY-MM

    # Trigger tracking
    dispatch_status: Mapped[str] = mapped_column(String(32), default="待派单", nullable=False, index=True)
    email_trigger_status: Mapped[str] = mapped_column(String(32), default="待发送", nullable=False, index=True)

    # Closure tracking: 未闭环 | 已闭环 | 闭环中 | 闭环失败
    closure_status: Mapped[str] = mapped_column(String(32), default="未闭环", nullable=False, index=True)
