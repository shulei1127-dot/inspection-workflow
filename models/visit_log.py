"""交付转售后回访闭环日志模型"""

import uuid

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VisitLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "visit_logs"

    # ── 关联 ──────────────────────────────────────────────────
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    review_log_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    pts_visit_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    # ── 项目信息（冗余，便于查询）──────────────────────────────
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 执行模式 ─────────────────────────────────────────────
    execution_mode: Mapped[str] = mapped_column(
        String(32), default="direct_http", nullable=False,
    )

    # ── 步骤状态 (pending / running / success / failed / skipped) ──
    step_fetch_delivery: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_create_visit: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_find_visit: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_fill_feedback: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_finish_visit: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_post_check: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )
    step_dingtalk_writeback: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False,
    )

    # ── 整体状态 (pending / running / completed / failed / partial) ──
    status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True, nullable=False,
    )

    # ── PTS 数据 ─────────────────────────────────────────────
    company_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    form_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 结果 ──────────────────────────────────────────────────
    visit_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    satisfaction_score: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    visit_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 钉钉写入 ──────────────────────────────────────────────
    dingtalk_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dingtalk_writeback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── 错误追踪 ─────────────────────────────────────────────
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    step_log: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # ── 触发来源 (auto / manual / retry) ──────────────────────
    trigger_source: Mapped[str] = mapped_column(
        String(32), default="auto", nullable=False,
    )
