"""巡检信息库：每个 PTS 巡检工单的可复用巡检信息快照。"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class InspectionInfoLibrary(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per PTS inspection work order with reusable inspection info.

    Used to auto-fill 巡检地址 / 报告发送邮箱 for new work orders that
    belong to the same 交付 (delivery_id) + 项目 (project_id).
    """

    __tablename__ = "inspection_info_library"

    pts_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    delivery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(512), nullable=True)
    on_site_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    aitable_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_inspection_library_delivery_project", "delivery_id", "project_id"),
    )
