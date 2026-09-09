"""巡检群自动拉机器人服务（方案1：定时扫描派单表「群ID」字段）。

流程：扫描巡检派单表中已回写「群ID」的行 -> 从 applink 链接解析 openConversationId
-> 检查「增值服务确认消息推送」机器人是否已在群（dws chat group bots）
-> 不在群则执行 add-bot。

execute=False（默认）时只扫描并记录“应拉群”，不真正执行拉机器人动作。
仅国家法定工作日运行（复用 dingtalk_notifier.is_workday_today）。
"""

from __future__ import annotations

import logging
import re
import urllib.parse

from services.dingtalk_client import _run_dws, query_records
from services.dingtalk_notifier import is_workday_today

logger = logging.getLogger(__name__)

# ===== 巡检派单记录表常量 =====
DISPATCH_BASE_ID = "YndMj49yWjPL7gq7TwPpArYyJ3pmz5aA"
DISPATCH_TABLE_ID = "UWdhzcr"
GROUP_FIELD_ID = "ygLtPYd"  # 群ID（文本：群名 + applink 链接）
CUSTOMER_FIELD_ID = "AkjEpbP"
PRODUCT_FIELD_ID = "XQJu8tp"

# ===== 机器人常量 =====
ROBOT_CODE = "dingi0fmclwiroilfgca"
ROBOT_NAME = "增值服务确认消息推送"

FIELD_IDS = ",".join([
    GROUP_FIELD_ID,
    CUSTOMER_FIELD_ID,
    PRODUCT_FIELD_ID,
])

_CONV_RE = re.compile(r"[?&]openConversationId=([^&\s]+)")


def extract_open_conversation_id(group_field_text) -> str | None:
    """从「群ID」字段文本（群名 + applink 链接）解析 openConversationId。

    例如：...?corpId=xxx&openConversationId=cid...%3D%3D -> cid...==
    """
    if not isinstance(group_field_text, str):
        return None
    text = group_field_text.replace("&amp;", "&")
    m = _CONV_RE.search(text)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1))


def _safe_str(val) -> str:
    if isinstance(val, str):
        return val
    return ""


def _is_robot_in_bots(bots, robot_code: str) -> bool:
    if not isinstance(bots, list):
        return False
    for bot in bots:
        if isinstance(bot, dict) and bot.get("robotCode") == robot_code:
            return True
    return False


async def _list_group_bots(conversation_id: str) -> list:
    """查询群内机器人列表，失败返回 []。"""
    result = await _run_dws([
        "chat", "group", "bots",
        "--group", conversation_id,
        "-f", "json",
        "-y",
    ])
    if not isinstance(result, dict):
        return []
    data = result.get("result")
    if isinstance(data, dict):
        bots = data.get("bots")
    else:
        bots = result.get("bots")
    if not isinstance(bots, list) and isinstance(result.get("data"), dict):
        bots = result.get("data", {}).get("bots")
    return bots if isinstance(bots, list) else []


async def _add_robot_to_group(conversation_id: str) -> tuple[bool, str | None]:
    """把机器人拉入群，返回 (是否成功, 错误信息)。"""
    result = await _run_dws([
        "chat", "group", "members", "add-bot",
        "--id", conversation_id,
        "--robot-code", ROBOT_CODE,
        "-f", "json",
        "-y",
    ])
    if not isinstance(result, dict):
        return False, "dws 无返回"
    if result.get("error"):
        return False, str(result.get("error"))
    if result.get("success") is False:
        return False, str(result.get("errorMessage") or result.get("error") or "unknown")
    if result.get("errorCode") not in (None, 0):
        return False, f"code={result.get('errorCode')} msg={result.get('errorMessage')}"
    return True, None


async def run_group_bot_sync(execute: bool = False) -> dict:
    """扫描派单表：机器人未入群的巡检群，execute=True 时真正拉机器人。

    execute=False（默认）只扫描并统计“应拉群”，不执行任何 add-bot。
    """
    if not is_workday_today():
        logger.info("group bot sync skipped: today is not a workday (holiday/weekend)")
        return {
            "status": "skipped_non_workday",
            "execute": execute,
            "scanned": 0,
            "with_group": 0,
            "already_in_group": 0,
            "to_add": 0,
            "added": 0,
            "failed": 0,
            "details": [],
        }

    records = await query_records(
        limit=100,
        base_id=DISPATCH_BASE_ID,
        table_id=DISPATCH_TABLE_ID,
        field_ids=FIELD_IDS,
        fetch_all=True,
    )
    if records is None:
        records = []

    stats = {
        "status": "ok",
        "execute": execute,
        "scanned": len(records),
        "with_group": 0,
        "already_in_group": 0,
        "to_add": 0,
        "would_add": 0,
        "added": 0,
        "failed": 0,
    }
    details: list[dict] = []

    for rec in records:
        cells = rec.get("cells", {}) or {}
        conversation_id = extract_open_conversation_id(cells.get(GROUP_FIELD_ID))
        if not conversation_id:
            continue
        stats["with_group"] += 1

        customer = _safe_str(cells.get(CUSTOMER_FIELD_ID))
        product = _safe_str(cells.get(PRODUCT_FIELD_ID))
        detail = {
            "record_id": rec.get("recordId"),
            "customer_name": customer,
            "product_name": product,
            "conversation_id": conversation_id,
        }

        bots = await _list_group_bots(conversation_id)
        if _is_robot_in_bots(bots, ROBOT_CODE):
            stats["already_in_group"] += 1
            detail["status"] = "already_in_group"
            details.append(detail)
            continue

        stats["to_add"] += 1
        if not execute:
            stats["would_add"] += 1
            detail["status"] = "would_add"
            detail["dry_run"] = True
            logger.info(
                "group bot sync(dry-run) would add: customer=%s product=%s conversation=%s robot=%s",
                customer, product, conversation_id, ROBOT_CODE,
            )
        else:
            ok, error = await _add_robot_to_group(conversation_id)
            if ok:
                stats["added"] += 1
                detail["status"] = "added"
                logger.info(
                    "group bot sync added robot: customer=%s product=%s conversation=%s",
                    customer, product, conversation_id,
                )
            else:
                stats["failed"] += 1
                detail["status"] = "failed"
                detail["error"] = error
                logger.warning(
                    "group bot sync add failed: customer=%s conversation=%s error=%s",
                    customer, conversation_id, error,
                )
        details.append(detail)

    return {**stats, "details": details}
