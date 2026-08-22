"""销售巡检确认推送服务。

每天扫描主表「审核通过 + 是否涉及增值服务=是」的新增记录，
给对应销售发送单聊消息（客户名称 + CRM项目 + 表单填写链接）。
销售在钉钉表单里提交「是否需要创建巡检工单 / 是否需要主动联系客户提供增值服务 /
不用创建巡检工单原因」后，结果会直接落在主表对应字段（表单视图挂载在主表上）。
本服务只负责推送 + 幂等记录，不重复打扰。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from core.config import get_settings
from core.db import SessionLocal

logger = logging.getLogger(__name__)

# ===== 主表字段常量 =====
BASE_ID = "o14dA3GK8g5LavPaT7dDQqoxV9ekBD76"
TABLE_ID = "Igz9TVd"

# 审核是否通过 (singleSelect)
AUDIT_RESULT_FIELD = "3np07ifl4yfr6jsxghcum"
AUDIT_PASS_OPTION_ID = "cnlzjhpNpy"  # 通过
AUDIT_REJECT_OPTION_ID = "1MO1Mgbfqu"  # 拒绝

# 是否涉及增值服务 (singleSelect) —— 由审核操作填写
VALUE_ADDED_FIELD = "bUQKF3E"
VALUE_ADDED_YES_OPTION_ID = "vdaDZWhK09"  # 是
VALUE_ADDED_NO_OPTION_ID = "t8KYKIkvto"  # 否

# 客户名称 / CRM项目 / 销售
CUSTOMER_NAME_FIELD = "rbiax8fi5eklvmdlc4v5d"
CRM_PROJECT_FIELD = "zmb66Mo"
SALES_USER_FIELD = "WLigZyI"

# 表单填写链接（已发布）
FORM_URL = "https://alidocs.dingtalk.com/notable/share/form/v014j6OJ5jPAGa8eq3p_Igz9TVd_gfxnoir"
# 钉钉机器人（增值服务确认消息推送，卡片发送权限已开通）
ROBOT_CODE = "dingi0fmclwiroilfgca"

FIELD_IDS = ",".join([
    AUDIT_RESULT_FIELD,
    VALUE_ADDED_FIELD,
    CUSTOMER_NAME_FIELD,
    CRM_PROJECT_FIELD,
    SALES_USER_FIELD,
])


def _field(cells: dict, field_id: str):
    return cells.get(field_id)


def _option_name(val) -> str | None:
    if isinstance(val, dict):
        return val.get("name")
    return None


def _extract_url(val) -> str | None:
    """url 类型字段 → 链接字符串。"""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("link") or val.get("text") or val.get("url")
    return None


def _extract_sales_user_ids(val) -> list[str]:
    """销售字段 (user, multiple) → [userId, ...]"""
    if not isinstance(val, list):
        return []
    ids = []
    for item in val:
        if isinstance(item, dict) and item.get("userId"):
            ids.append(item["userId"])
    return ids


async def _send_dingtalk_message(user_id: str, text: str, title: str) -> bool:
    """通过钉钉机器人（增值服务确认消息推送）给指定销售发单聊消息。"""
    import services.dingtalk_client as dc
    result = await dc._run_dws([
        "chat", "message", "send-by-bot",
        "--robot-code", ROBOT_CODE,
        "--users", user_id,
        "--title", title,
        "--text", text,
        "-y", "-f", "json",
    ])
    if result is None:
        return False
    # 成功判定：success=true 且 errorCode=0
    if isinstance(result, dict):
        if result.get("error"):
            logger.warning("send message failed: %s", result.get("error"))
            return False
        if result.get("success") is False:
            logger.warning("send message failed: %s", result.get("errorMessage") or result.get("error"))
            return False
        if result.get("errorCode") not in (None, 0):
            logger.warning("send message failed: code=%s msg=%s", result.get("errorCode"), result.get("errorMessage"))
            return False
    return True


def _build_message(customer_name: str, crm_url: str) -> str:
    lines = [
        "【巡检工单创建确认 · 由AI代发】",
        f"您好，客户「{customer_name or "—"}」已完成交付转售后审核，且服务涉及增值服务。",
        "请确认是否需要主动联系客户创建巡检工单：",
        "",
    ]
    if crm_url:
        lines.append(f"📌 CRM项目：{crm_url}")
    lines.append(f"📝 请点击填写确认表：{FORM_URL}")
    return "\n".join(lines)


async def run_sales_confirm(dry_run: bool = True) -> dict:
    """扫描主表并推送巡检确认表单给对应销售。

    dry_run=True 时只扫描、不实际发送消息。
    """
    from services.dingtalk_client import query_records
    from models.sales_confirm_log import SalesConfirmLog

    settings = get_settings()
    scan = await query_records(
        limit=100,
        base_id=BASE_ID,
        table_id=TABLE_ID,
        field_ids=FIELD_IDS,
        fetch_all=True,
    )

    candidates = []
    for rec in scan:
        cells = rec.get("cells", {}) or {}
        audit = _option_name(cells.get(AUDIT_RESULT_FIELD))
        value_added = _option_name(cells.get(VALUE_ADDED_FIELD))
        if audit == "通过" and value_added == "是":
            candidates.append(rec)

    sent = 0
    skipped = 0
    errors = 0
    details = []

    with SessionLocal() as db:
        for rec in candidates:
            record_id = rec.get("recordId")
            cells = rec.get("cells", {}) or {}
            customer_name = _field(cells, CUSTOMER_NAME_FIELD)
            crm_url = _extract_url(_field(cells, CRM_PROJECT_FIELD))
            sales_ids = _extract_sales_user_ids(cells.get(SALES_USER_FIELD))

            if not sales_ids:
                skipped += 1
                details.append({"record_id": record_id, "customer_name": customer_name, "status": "no_sales"})
                continue

            user_id = sales_ids[0]
            message = _build_message(customer_name, crm_url)
            details.append({
                "record_id": record_id,
                "customer_name": customer_name,
                "crm_project_url": crm_url,
                "sales_user_id": user_id,
                "message": message,
                "status": "will_send" if dry_run else "sent",
                "dry_run": dry_run,
            })

            # dry_run 不写库、不发送；真实运行时才做幂等与推送
            if dry_run:
                continue

            existing = db.query(SalesConfirmLog).filter(
                SalesConfirmLog.record_id == record_id
            ).first()
            if existing:
                skipped += 1
                details.append({"record_id": record_id, "customer_name": customer_name, "status": "skipped(already)"})
                continue

            ok = await _send_dingtalk_message(user_id, message, "巡检工单创建确认")
            log = SalesConfirmLog(
                record_id=record_id,
                customer_name=customer_name,
                crm_project_url=crm_url,
                sales_user_id=user_id,
                status="sent" if ok else "error",
                sent_at=datetime.now(timezone.utc) if ok else None,
                snapshot={"cells": cells, "message": message},
                error=None if ok else "发送失败",
            )
            db.add(log)
            if ok:
                sent += 1
            else:
                errors += 1

        db.commit()

    return {
        "status": "ok",
        "dry_run": dry_run,
        "scanned": len(scan),
        "candidates": len(candidates),
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }
