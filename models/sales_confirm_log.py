"""销售巡检确认推送日志模型。

记录已向销售推送「巡检工单创建确认」表单的记录，避免重复打扰。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class SalesConfirmLog(Base, TimestampMixin):
    __tablename__ = "sales_confirm_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, unique=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_project_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sales_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 推送状态: sent = 已推送, replied = 已收到表单提交
    status: Mapped[str] = mapped_column(String(32), default="sent", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 表单提交回读结果（对应主表 3 个字段）
    need_create_inspection: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 是否需要创建巡检工单
    need_contact_value_added: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 是否需要主动联系客户提供增值服务
    no_create_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # 不用创建巡检工单原因/特殊情况备注

    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
