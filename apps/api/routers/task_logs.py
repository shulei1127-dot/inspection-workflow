"""Unified task log endpoints — aggregate all task execution logs."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.utils import fmt_cst
from core.db import get_db
from models.sync_log import SyncLog
from models.trigger_log import TriggerLog
from models.email_pre_analysis import EmailPreAnalysis
from models.change_log import ChangeLog

router = APIRouter(tags=["task-logs"])

# Map task_type values to human-readable labels
_TASK_TYPE_MAP = {
    "sync_full": "PTS完整同步",
    "sync_fetch": "PTS数据拉取",
    "sync_push": "AITable推送",
    "sync_batch_push": "批量推送",
    "dispatch": "云集派单",
    "email": "巡检邮件",
    "closure": "工单闭环",
    "email_pre_analysis": "邮件预分析",
    "change_summary": "变更日报推送",
}

_TRIGGER_TYPE_TO_TASK = {
    "yunji_dispatch": "dispatch",
    "inspection_email": "email",
    "closure_success": "closure",
    "closure_failed": "closure",
    "closure_manual": "closure",
}

_SYNC_TYPE_TO_TASK = {
    "full_sync": "sync_full",
    "fetch_only": "sync_fetch",
    "push_only": "sync_push",
    "batch_push": "sync_batch_push",
}

# Impossible condition to exclude all rows when task_type doesn't match
_NEVER = text("0 = 1")


@router.get("/api/task-logs")
async def get_task_logs(
    task_type: str | None = Query(None, description="按任务类型筛选"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Aggregate all task execution logs into a unified timeline."""
    items: list[dict] = []

    # 1. SyncLog — 同步类日志
    sync_query = db.query(SyncLog).order_by(SyncLog.started_at.desc())
    if task_type and task_type.startswith("sync_"):
        sync_type = {v: k for k, v in _SYNC_TYPE_TO_TASK.items()}.get(task_type)
        if sync_type:
            sync_query = sync_query.filter(SyncLog.sync_type == sync_type)
        else:
            sync_query = sync_query.filter(_NEVER)
    elif task_type:
        sync_query = sync_query.filter(_NEVER)

    for log in sync_query.limit(limit).all():
        task_key = _SYNC_TYPE_TO_TASK.get(log.sync_type, "sync_full")
        items.append({
            "id": str(log.id),
            "task_type": task_key,
            "task_label": _TASK_TYPE_MAP.get(task_key, task_key),
            "trigger_source": "定时" if log.trigger_source == "scheduler" else "手动",
            "status": log.status,
            "time": fmt_cst(log.started_at),
            "completed_at": fmt_cst(log.completed_at),
            "summary": f"月份{log.sync_month} | 拉取{log.fetched_count} 新建{log.created_count} 更新{log.updated_count} 跳过{log.skipped_count}",
            "error": log.error_message,
            "source_table": "sync_log",
        })

    # 2. TriggerLog — 派单/邮件/闭环
    trigger_query = db.query(TriggerLog).order_by(TriggerLog.created_at.desc())
    if task_type and task_type in ("dispatch", "email", "closure"):
        trigger_types = [k for k, v in _TRIGGER_TYPE_TO_TASK.items() if v == task_type]
        trigger_query = trigger_query.filter(TriggerLog.trigger_type.in_(trigger_types))
    elif task_type:
        trigger_query = trigger_query.filter(_NEVER)

    for log in trigger_query.limit(limit).all():
        task_key = _TRIGGER_TYPE_TO_TASK.get(log.trigger_type, log.trigger_type)
        items.append({
            "id": str(log.id),
            "task_type": task_key,
            "task_label": _TASK_TYPE_MAP.get(task_key, task_key),
            "trigger_source": "手动",
            "status": log.status,
            "time": fmt_cst(log.created_at),
            "completed_at": fmt_cst(log.completed_at),
            "summary": log.trigger_reason or log.trigger_type,
            "error": None,
            "source_table": "trigger_log",
        })

    # 3. EmailPreAnalysis — 预分析
    ep_query = db.query(EmailPreAnalysis).order_by(EmailPreAnalysis.created_at.desc())
    if task_type and task_type != "email_pre_analysis":
        ep_query = ep_query.filter(_NEVER)

    for log in ep_query.limit(limit).all():
        status_cn = {"success": "成功", "failed": "失败", "pending": "进行中"}.get(log.analysis_status, log.analysis_status)
        items.append({
            "id": str(log.id),
            "task_type": "email_pre_analysis",
            "task_label": "邮件预分析",
            "trigger_source": "定时",
            "status": status_cn,
            "time": fmt_cst(log.created_at),
            "completed_at": fmt_cst(log.analyzed_at),
            "summary": f"{log.customer_name or ''} {log.product_name or ''}",
            "error": log.error_message,
            "source_table": "email_pre_analysis",
        })

    # 4. ChangeLog push records — 变更日报推送
    change_query = db.query(ChangeLog).filter(ChangeLog.pushed_to_dingtalk == True).order_by(ChangeLog.pushed_at.desc())
    if task_type and task_type != "change_summary":
        change_query = change_query.filter(_NEVER)

    for log in change_query.limit(limit).all():
        items.append({
            "id": str(log.id),
            "task_type": "change_summary",
            "task_label": "变更日报推送",
            "trigger_source": "定时",
            "status": "成功",
            "time": fmt_cst(log.pushed_at),
            "completed_at": fmt_cst(log.pushed_at),
            "summary": f"{log.change_date} {log.title}",
            "error": None,
            "source_table": "change_log",
        })

    # Sort all items by time descending
    items.sort(key=lambda x: x["time"] or "", reverse=True)

    return {"total": len(items), "items": items[:limit]}
