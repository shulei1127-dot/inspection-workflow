"""Agent Hub（support-ai 纳管平台）脱敏快照上报模块。

运行契约：
- 仅在 AGENT_HUB_ENABLED=true 且存在可用 Token 时生效，默认关闭；
- Token 通过 AGENT_HUB_TOKEN 环境变量或 AGENT_HUB_TOKEN_FILE 指向的
  root-only 密钥文件注入，绝不进入 Git、数据库、日志或命令行；
- 每轮严格按 verify → register/update → snapshot 顺序执行，任一步非 2xx
  立即中止本轮并抛出异常，保留 Hub 上一份快照（不会删除/覆盖旧快照）；
- 只上报脱敏聚合指标，不包含工单/项目/人员明细，请求体与 Token 永不写日志。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_FRESHNESS_SLA_MINUTES = 1440  # 巡检工单同步为每日任务，SLA 24 小时

# 指标白名单：新增指标必须同时更新本白名单与 tests/test_agent_hub_reporter.py
METRIC_KEYS = (
    "work_order_tracked_count",
    "work_order_synced_count",
    "monthly_sync_association_count",
    "review_audit_processed_7d_count",
    "review_audit_manual_7d_count",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(db, clause) -> int:
    return int(db.execute(clause).scalar() or 0)


def resolve_token(settings) -> str:
    """优先读取 AGENT_HUB_TOKEN，其次读取 root-only 密钥文件（不写日志）。"""
    if settings.agent_hub_token:
        return settings.agent_hub_token
    token_file = (settings.agent_hub_token_file or "").strip()
    if not token_file:
        return ""
    try:
        with open(token_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        logger.warning("Agent Hub token file unreadable: %s", exc.strerror or exc.__class__.__name__)
        return ""


def is_configured(settings) -> bool:
    return bool(
        settings.agent_hub_enabled
        and settings.agent_hub_agent_key.strip()
        and (settings.agent_hub_token or (settings.agent_hub_token_file or "").strip())
    )


def idempotency_key(agent_key: str, snapshot_id: str) -> str:
    """同一 (agent_key, snapshot_id) 幂等；重试相同快照不产生重复写入。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{agent_key}:{snapshot_id}"))


def build_snapshot(db) -> dict:
    """从本地 PostgreSQL 构造 aggregated_sanitized 快照（只读聚合）。"""
    from sqlalchemy import text

    settings = get_settings()
    now = datetime.now(timezone.utc)

    wo_total = _count(db, text("SELECT count(*) FROM work_orders"))
    wo_synced = _count(db, text("SELECT count(*) FROM work_orders WHERE dt_sync_status = 'synced'"))
    ws_total = _count(db, text("SELECT count(*) FROM work_order_syncs"))
    audit_7d = _count(db, text(
        "SELECT count(*) FROM review_audit_logs "
        "WHERE created_at > now() - interval '7 days' AND conclusion <> 'error'"
    ))
    manual_7d = _count(db, text(
        "SELECT count(*) FROM review_audit_logs "
        "WHERE created_at > now() - interval '7 days' AND conclusion = '转人工审核'"
    ))

    last_success = db.execute(text(
        "SELECT max(started_at) FROM sync_logs WHERE status IN ('success', 'partial')"
    )).scalar()
    last_success_at = _iso(last_success) if last_success else None
    age_minutes = (now - last_success).total_seconds() / 60.0 if last_success else None

    if last_success_at is None:
        status, stale = "offline", True
    elif age_minutes is not None and age_minutes <= _FRESHNESS_SLA_MINUTES:
        status, stale = "healthy", False
    elif age_minutes is not None and age_minutes <= _FRESHNESS_SLA_MINUTES * 3:
        status, stale = "degraded", True
    else:
        status, stale = "offline", True

    metrics = [
        {
            "key": "work_order_tracked_count",
            "label": "本地跟踪巡检工单数",
            "value": wo_total,
            "unit": "orders",
            "definition": "本地库中已跟踪的巡检/日志分析类工单总数，含已闭环与未闭环；只传数量，不传工单身份或内容。",
            "source": "inspection-workflow.work_orders",
            "refresh_frequency": "每 30 分钟",
            "threshold": "数量突变时复核 PTS 查询与过滤口径",
        },
        {
            "key": "work_order_synced_count",
            "label": "已同步钉钉派单表工单数",
            "value": wo_synced,
            "unit": "orders",
            "definition": "本地库标记为已同步到钉钉巡检派单表的工单数。",
            "source": "inspection-workflow.work_orders",
            "refresh_frequency": "每 30 分钟",
            "threshold": "低于本地跟踪数时检查同步失败原因",
        },
        {
            "key": "monthly_sync_association_count",
            "label": "月度同步关联记录数",
            "value": ws_total,
            "unit": "records",
            "definition": "工单月度派单关联记录（work_order_syncs）总数，反映钉钉派单表行与工单的月度映射规模。",
            "source": "inspection-workflow.work_order_syncs",
            "refresh_frequency": "每 30 分钟",
            "threshold": "连续多日无新增或整批激增时由负责人复核",
        },
        {
            "key": "review_audit_processed_7d_count",
            "label": "近 7 日交付转售后自动审核处理数",
            "value": audit_7d,
            "unit": "projects",
            "definition": "最近 7 个自然日交付转售后审核流水线处理的项目数（含自动通过、不通过与转人工，不含异常）。",
            "source": "inspection-workflow.review_audit_logs",
            "refresh_frequency": "每 30 分钟",
            "threshold": "工作日处理量突变为 0 时检查流水线是否停摆",
        },
        {
            "key": "review_audit_manual_7d_count",
            "label": "近 7 日转人工审核数",
            "value": manual_7d,
            "unit": "projects",
            "definition": "最近 7 个自然日审核规则判定需人工复核并转人工的项目数。",
            "source": "inspection-workflow.review_audit_logs",
            "refresh_frequency": "每 30 分钟",
            "threshold": "占比异常升高时复核规则边界与关键产品清单",
        },
    ]

    suggestions = []
    if status != "healthy":
        age_txt = f"{int(age_minutes)} 分钟" if age_minutes is not None else "未知"
        suggestions.append({
            "title": "巡检工单同步新鲜度异常",
            "detail": f"最近一次成功同步距今 {age_txt}，超过新鲜度 SLA，请检查 PTS 查询/限流与定时任务。",
            "status": "watch",
            "basis": "依据为 sync_logs 最近成功时间与新鲜度 SLA 的比对结果",
        })

    observed_at = _iso(now)
    snapshot_id = f"{settings.agent_hub_agent_key}-{now.strftime('%Y%m%dT%H%MZ')}"
    return {
        "schema_version": "v1",
        "agent_key": settings.agent_hub_agent_key,
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "source_updated_at": last_success_at or observed_at,
        "data_classification": "aggregated_sanitized",
        "health": {
            "status": status,
            "last_success_at": last_success_at or observed_at,
            "freshness_sla_minutes": _FRESHNESS_SLA_MINUTES,
        },
        "metrics": metrics,
        "suggestions": suggestions,
    }


async def report_once(db, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """执行一轮 verify → register/update → snapshot 上报。

    未启用或无 Token 时直接返回 disabled，不发起任何请求；
    任一步非 2xx 立即中止本轮（raise），保留 Hub 上一份快照。
    transport 仅供单测注入 MockTransport，生产恒为 None（默认自动连接）。
    """
    settings = get_settings()
    agent_key = settings.agent_hub_agent_key.strip()
    if not is_configured(settings):
        return {"status": "disabled", "agent_key": agent_key or "unset"}

    token = resolve_token(settings)
    if not token:
        logger.warning("Agent Hub enabled but no token available; round skipped")
        return {"status": "disabled", "agent_key": agent_key}

    base = settings.agent_hub_api_base.rstrip("/")
    snapshot = build_snapshot(db)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key(agent_key, snapshot["snapshot_id"]),
    }
    endpoints = [
        ("POST", f"{base}/agents/verify", {"agent_key": agent_key}),
        ("PUT", f"{base}/agents/{agent_key}", {"agent_key": agent_key}),
        ("POST", f"{base}/agents/{agent_key}/snapshots", snapshot),
    ]

    async with httpx.AsyncClient(timeout=settings.agent_hub_timeout_seconds, transport=transport) as client:
        for method, url, payload in endpoints:
            resp = await client.request(method, url, json=payload, headers=headers)
            if resp.status_code >= 400:
                detail = (resp.text or "")[:200].replace("\n", " ")
                raise RuntimeError(
                    f"Agent Hub {method} {url.rsplit('/', 1)[-1]} -> HTTP {resp.status_code}: {detail}"
                )
    return {"status": "ok", "agent_key": agent_key, "snapshot_id": snapshot["snapshot_id"]}
