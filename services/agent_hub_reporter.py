"""Agent Hub（support-ai 纳管平台）脱敏快照上报模块。

运行契约（对齐 Agent Hub v1 接入契约，见 agent-hub-onboarding README）：
- 仅在 AGENT_HUB_ENABLED=true 且存在可用 Token 时生效，默认关闭；
- Token 通过 AGENT_HUB_TOKEN 环境变量或 AGENT_HUB_TOKEN_FILE 指向的
  root-only 密钥文件注入，绝不进入 Git、数据库、日志或命令行；
- 每轮严格按 verify → register/update → snapshot 顺序执行：
    POST /agents/verify                  body={"manifest": <完整 manifest>}
    PUT  /agents/{agent_key}             body=<完整 manifest>
    POST /agents/{agent_key}/snapshots   body=<脱敏快照>
  任一步非 2xx 立即中止本轮并抛出异常，保留 Hub 上一份快照；
- Agent Hub v1 只接受 health.status=healthy；同步过期用 health.stale=true
  与 watch 建议表达，不发送 offline/degraded；
- 只上报脱敏聚合指标，不包含工单/项目/人员明细，请求体与 Token 永不写日志。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_FRESHNESS_SLA_MINUTES = 1440  # 巡检工单同步为每日任务，SLA 24 小时
_MAX_PAYLOAD_BYTES = 262144  # Agent Hub 契约限制

# 指标白名单：新增指标必须同时更新本白名单、
# tests/test_agent_hub_reporter.py 与 agents/support.inspection-workflow/snapshot.sample.json
METRIC_KEYS = (
    "work_order_tracked_count",
    "work_order_synced_count",
    "monthly_sync_association_count",
    "review_audit_processed_7d_count",
    "review_audit_manual_7d_count",
)

# 完整 manifest：必须与 agent-hub-onboarding agents/support.inspection-workflow/manifest.json 保持一致
_AGENT_MANIFEST: dict = {
    "schema_version": "v1",
    "agent_key": "support.inspection-workflow",
    "display_name": "主动服务流程自动化运行助手",
    "description": "巡检工单流程自动化服务运行在内部 dev-box（10.2.36.228:8100），覆盖巡检工单 PTS 同步、钉钉巡检派单表维护、交付转售后自动审核、巡检报告邮件预分析、工单闭环推进与巡检信息库回写等运营链路。服务对象为巡检运营、售后与交付团队；输入为 PTS 工单数据与钉钉派单表状态，输出为脱敏聚合运行指标与看板，业务明细与客户原文保留在原系统权限边界内，不向 Agent Hub 上报。",
    "owner": "舒磊｜巡检工单流程自动化负责人",
    "dashboard_url": "http://10.2.36.228:8100/",
    "capabilities": [
        "展示巡检工单同步、钉钉派单表、交付转售后自动审核、工单闭环、邮件预分析与巡检信息库的聚合运行状态，不下发逐条业务记录",
        "仅上报 aggregated_sanitized 的计数与健康指标，不包含工单内容、项目名称、人员信息或原文",
        "指标同时声明统计口径、数据来源、刷新频率与异常阈值，供集中广场展示与复核",
    ],
    "workflow": [
        "每日同步 PTS 巡检工单到本地库并维护钉钉巡检派单表（月度关联按工单+月份幂等去重）",
        "交付转售后审核流水线按规则自动审核，License 缺失时按机器码关联自动补全并回写 PTS，规则外项目转人工审核",
        "巡检报告邮件预分析与手动发送模块生成发送预览与状态，报告上传与工单闭环按邮件状态自动推进",
        "运行期间读取本地 PostgreSQL 状态源，按固定指标白名单生成脱敏快照，上报失败保留本地并在下一周期重试，不影响业务处理",
    ],
    "automations": [
        "巡检工单同步定时任务（工作日 16:00）与派单表监控轮询",
        "交付转售后审核流水线（工作日 16:05，法定节假日自动跳过）",
        "邮件预分析（每日 9:00）、工单闭环检查（每日 20:00）、巡检信息库同步回写（每日 18:00）",
        "Agent Hub 快照上报仅在显式启用且注入服务 Token 时运行，默认关闭；Token 不写入 Git、数据库或日志",
    ],
    "data_classification": "aggregated_sanitized",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(db, clause) -> int:
    return int(db.execute(clause).scalar() or 0)


def manifest_for(settings) -> dict:
    """返回上报用的完整 manifest（可在部署时用 AGENT_HUB_DASHBOARD_URL 覆盖看板地址）。"""
    manifest = dict(_AGENT_MANIFEST)
    dashboard_url = (getattr(settings, "agent_hub_dashboard_url", "") or "").strip()
    if dashboard_url:
        manifest["dashboard_url"] = dashboard_url
    return manifest


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


def _payload_digest(payload: dict) -> str:
    """载荷的确定性摘要，用于生成幂等键（同载荷同键，重试不重复写入）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def idempotency_key(agent_key: str, identity: str, payload: dict) -> str:
    """每个端点一个稳定 Idempotency-Key：同 (端点身份, 载荷) 幂等。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{agent_key}:{identity}:{_payload_digest(payload)}"))


def build_snapshot(db) -> dict:
    """从本地 PostgreSQL 构造 aggregated_sanitized 快照（只读聚合）。

    健康表达：Agent Hub v1 只接受 health.status=healthy；本地同步新鲜度
    （SLA 24 小时）过期时置 health.stale=true 并附加 watch 建议，不发 offline。
    """
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

    stale = last_success_at is None or (age_minutes is not None and age_minutes > _FRESHNESS_SLA_MINUTES)

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
    if stale:
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
            "status": "healthy",
            "stale": stale,
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

    manifest = manifest_for(settings)
    if not manifest.get("agent_key") or manifest["agent_key"] != agent_key:
        raise RuntimeError("Agent Hub manifest agent_key mismatch")
    if manifest.get("data_classification") != "aggregated_sanitized":
        raise RuntimeError("Agent Hub manifest data_classification must be aggregated_sanitized")

    base = settings.agent_hub_api_base.rstrip("/")
    snapshot = build_snapshot(db)
    snapshot_body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    if len(snapshot_body) > _MAX_PAYLOAD_BYTES:
        raise RuntimeError("Agent Hub snapshot payload exceeds %d bytes" % _MAX_PAYLOAD_BYTES)

    steps = [
        ("POST", f"{base}/agents/verify", {"manifest": manifest}, "verify"),
        ("PUT", f"{base}/agents/{agent_key}", manifest, "register"),
        ("POST", f"{base}/agents/{agent_key}/snapshots", snapshot, "snapshot"),
    ]

    async with httpx.AsyncClient(timeout=settings.agent_hub_timeout_seconds, transport=transport) as client:
        for method, url, payload, identity in steps:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key(agent_key, identity, payload),
            }
            resp = await client.request(method, url, json=payload, headers=headers)
            if resp.status_code >= 400:
                detail = (resp.text or "")[:200].replace("\n", " ")
                raise RuntimeError(
                    f"Agent Hub {method} {url.rsplit('/', 1)[-1]} -> HTTP {resp.status_code}: {detail}"
                )
    return {"status": "ok", "agent_key": agent_key, "snapshot_id": snapshot["snapshot_id"]}
