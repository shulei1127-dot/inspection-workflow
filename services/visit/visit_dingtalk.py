"""交付转售后回访闭环 — 钉钉数据表写入

将回访结果写回钉钉 AITable：
- 更新已有审核记录的回访链接字段
- 或创建新的 AITable 记录（如无对应审核记录）
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import get_settings
from services.dingtalk_client import create_records, update_records

logger = logging.getLogger(__name__)


async def update_visit_link_to_dingtalk(
    *,
    project_id: str,
    visit_url: str,
    dingtalk_record_id: str | None = None,
    customer_name: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """将回访链接写回钉钉 AITable。

    Args:
        project_id: PTS 交付 ID
        visit_url: 回访工单链接
        dingtalk_record_id: 已有的 AITable 记录 ID（如有）
        customer_name: 客户名称（创建新记录时用）
        region: 区域（创建新记录时用）

    Returns:
        写入结果字典
    """
    settings = get_settings()

    if not settings.visit_writeback_enabled:
        return {"action": "disabled", "reason": "visit_writeback_enabled is False"}

    # 与读取路径保持一致：优先 visit_writeback 配置，回退到 review AITable
    base_id = settings.visit_writeback_aitable_base_id or settings.review_aitable_base_id
    table_id = settings.visit_writeback_aitable_table_id or settings.review_aitable_main_table_id
    link_field_id = settings.visit_writeback_link_field_id

    if not base_id or not table_id or not link_field_id:
        logger.warning("钉钉回访写入配置不完整: base_id=%s, table_id=%s, field_id=%s", base_id, table_id, link_field_id)
        return {"action": "skipped", "reason": "配置不完整"}

    try:
        # 如果有已有的 AITable 记录 ID，直接更新
        if dingtalk_record_id:
            await update_records(
                [{"recordId": dingtalk_record_id, "cells": {link_field_id: visit_url}}],
                base_id=base_id,
                table_id=table_id,
            )
            logger.info("更新回访链接到已有 AITable 记录: record_id=%s", dingtalk_record_id)
            return {"action": "updated", "record_id": dingtalk_record_id}

        # 否则创建新记录
        cells: dict[str, Any] = {
            link_field_id: visit_url,
        }
        if customer_name:
            cells["客户名称"] = customer_name
        if region:
            cells["区域"] = region
        pts_url = f"https://pts.chaitin.net/project/{project_id}#base"
        cells["PTS交付链接"] = {"link": pts_url, "text": pts_url}

        resp = await create_records(
            [{"cells": cells}],
            base_id=base_id,
            table_id=table_id,
        )
        if not resp:
            logger.warning("钉钉回访写入失败: 无响应")
            return {"action": "failed", "reason": "no response"}

        new_record_id = (resp.get("newRecordIds") or [None])[0]
        logger.info("创建回访链接 AITable 记录: record_id=%s", new_record_id)
        return {"action": "created", "record_id": new_record_id}

    except Exception as e:
        logger.error("钉钉回访写入失败: %s", e)
        return {"action": "failed", "error": str(e)}
