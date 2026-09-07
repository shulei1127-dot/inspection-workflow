"""Statistics API: aggregation queries for the frontend dashboard.

数据概览统一以钉钉 AI 数据表《客户巡检派单》为唯一事实源，按每条记录的
「记录时间」月份归集。本地 work_orders.planned_completion 只反映工单当前
计划完成日期，无法还原钉钉表每月新增的历史行，因此不再用于月度统计。

业务口径（基于钉钉表字段）：
- 工单总数   = 所选月份记录行数
- 已派单     = 需求单号非空（云集派单成功后写回需求单号/订单编号）
- 待派单     = 工单总数 - 已派单（需求单号为空）
- 可立即派单 = 待派单中 伙伴供应商+伙伴负责人+工程师 均已填（满足云集派单条件）
- 已发邮件   = 邮件是否发送 = 是
- 待发邮件   = 未发送且未标记「不涉及」；其中报告已上传=可发未发，报告未上传=报告未就绪
- 已闭环     = 工单是否闭环 = 是

Endpoints:
- GET /api/statistics/overview      — 月度核心指标
- GET /api/statistics/by-region     — 月度区域分布
- GET /api/statistics/by-status     — 月度派单/邮件状态（饼图，扇区合计=工单总数）
- GET /api/statistics/monthly-trend — 近 6 个月工单量与闭环量
- GET /api/statistics/triggers      — 触发日志成功/失败统计（本地库，独立口径）
"""

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import get_db
from models.trigger_log import TriggerLog
from services import dingtalk_client
from services.aitable_fields import (
    DISPATCH,
    current_month,
    extract_engineer,
    extract_select_name,
    extract_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["statistics"])

# 仅拉取概览需要的字段，降低 dws 查询耗时与传输量
_STATS_FIELD_IDS = ",".join([
    DISPATCH["记录时间"],
    DISPATCH["所属区域"],
    DISPATCH["需求编号"],
    DISPATCH["邮件是否发送"],
    DISPATCH["巡检报告"],
    DISPATCH["巡检是否完成"],
    DISPATCH["工单是否闭环"],
    DISPATCH["伙伴供应商"],
    DISPATCH["伙伴负责人"],
    DISPATCH["工程师"],
])

_FETCH_TTL_SECONDS = 30  # 同一份钉钉表数据在窗口期内复用，避免频繁调用 dws

_fetch_cache: dict = {"ts": 0.0, "records": []}
_fetch_lock: asyncio.Lock | None = None


def _normalize_month(month: str | None) -> str:
    month = month or current_month()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=400, detail="month 参数格式须为 YYYY-MM")
    return month


async def _load_dispatch_records() -> list[dict]:
    """Fetch all 客户巡检派单 records once per TTL (single-flight)."""
    global _fetch_lock
    if time.time() - _fetch_cache["ts"] < _FETCH_TTL_SECONDS:
        return _fetch_cache["records"]

    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        raise HTTPException(status_code=503, detail="客户巡检派单 AITable 未配置")

    if _fetch_lock is None:
        _fetch_lock = asyncio.Lock()

    async with _fetch_lock:
        if time.time() - _fetch_cache["ts"] < _FETCH_TTL_SECONDS:  # double-check
            return _fetch_cache["records"]
        try:
            records = await dingtalk_client.query_records(
                limit=100,
                fetch_all=True,
                field_ids=_STATS_FIELD_IDS,
                base_id=settings.dt_dispatch_base_id,
                table_id=settings.dt_dispatch_table_id,
                strict=True,
            )
        except Exception as exc:
            logger.warning("AITable fetch failed in statistics: %s", exc)
            if _fetch_cache["records"]:
                # 数据源短暂不可用时保留上一次成功数据，避免展示虚假的 0
                return _fetch_cache["records"]
            raise HTTPException(status_code=502, detail="钉钉数据表获取失败，请稍后重试") from exc
        if not records:
            logger.warning("AITable returned no records in statistics")
            if _fetch_cache["records"]:
                return _fetch_cache["records"]
            raise HTTPException(status_code=502, detail="钉钉数据表获取失败，请稍后重试")
        _fetch_cache = {"ts": time.time(), "records": records}
        return records


def _cells_of(record: dict) -> dict:
    cells = record.get("cells") or {}
    return cells if isinstance(cells, dict) else {}


def _record_month(cells: dict) -> str | None:
    """记录时间字段 → YYYY-MM（日期字段可能返回字符串或富文本结构）。"""
    val = cells.get(DISPATCH["记录时间"])
    if val is None:
        return None
    if isinstance(val, str):
        text = val
    elif isinstance(val, dict):
        text = val.get("text") or val.get("value") or ""
    elif isinstance(val, list) and val:
        return _record_month({DISPATCH["记录时间"]: val[0]})
    else:
        text = str(val)
    match = re.match(r"(\d{4}-\d{2})", text.strip())
    return match.group(1) if match else None


def _empty_month() -> dict:
    return {
        "total": 0,
        "dispatched": 0,
        "dispatch_ready": 0,
        "emailed": 0,
        "email_na": 0,
        "email_ready": 0,
        "report_missing": 0,
        "completed": 0,
        "closed": 0,
        "regions": Counter(),
    }


def _aggregate_by_month(records: list[dict]) -> tuple[dict[str, dict], int]:
    """按记录时间月份聚合派单/邮件/闭环等状态，返回 (month→agg, 未归集条数)。"""
    months: dict[str, dict] = {}
    unattributed = 0
    for record in records:
        cells = _cells_of(record)
        month = _record_month(cells)
        if not month:
            unattributed += 1
            continue
        agg = months.setdefault(month, _empty_month())
        agg["total"] += 1

        demand = bool((extract_text(cells.get(DISPATCH["需求编号"])) or "").strip())
        supplier = bool((extract_select_name(cells.get(DISPATCH["伙伴供应商"])) or "").strip())
        manager = bool((extract_engineer(cells.get(DISPATCH["伙伴负责人"])) or "").strip())
        engineer = bool((extract_engineer(cells.get(DISPATCH["工程师"])) or "").strip())
        region = extract_select_name(cells.get(DISPATCH["所属区域"])) or "未分配"
        agg["regions"][region] += 1

        email = extract_select_name(cells.get(DISPATCH["邮件是否发送"]))
        report_val = cells.get(DISPATCH["巡检报告"])
        has_report = isinstance(report_val, list) and bool(report_val)

        if demand:
            agg["dispatched"] += 1
        elif supplier and manager and engineer:
            agg["dispatch_ready"] += 1

        if email == "是":
            agg["emailed"] += 1
        elif email == "不涉及":
            agg["email_na"] += 1
        elif has_report:
            agg["email_ready"] += 1  # 报告已就绪，可发未发
        else:
            agg["report_missing"] += 1  # 报告未上传

        if extract_select_name(cells.get(DISPATCH["巡检是否完成"])) == "是":
            agg["completed"] += 1
        if extract_select_name(cells.get(DISPATCH["工单是否闭环"])) == "是":
            agg["closed"] += 1
    return months, unattributed


def _previous_months(month: str, count: int) -> list[str]:
    year, mon = int(month[:4]), int(month[5:7])
    months: list[str] = []
    for _ in range(count):
        months.append(f"{year:04d}-{mon:02d}")
        mon -= 1
        if mon == 0:
            year -= 1
            mon = 12
    months.reverse()
    return months


def _month_range(month: str) -> tuple[date, date]:
    """Return (start_date, end_date) for a YYYY-MM month string."""
    import calendar
    year, m = month.split("-")
    start = date(int(year), int(m), 1)
    last_day = calendar.monthrange(int(year), int(m))[1]
    end = date(int(year), int(m), last_day)
    return start, end


@router.get("/api/statistics/overview")
async def statistics_overview(
    month: str | None = Query(None, description="YYYY-MM, defaults to current month"),
):
    """月度核心指标（口径见模块注释，来源：钉钉客户巡检派单表）。"""
    month = _normalize_month(month)
    records = await _load_dispatch_records()
    months, unattributed = _aggregate_by_month(records)
    agg = months.get(month, _empty_month())

    dispatched = agg["dispatched"]
    total = agg["total"]
    email_ready = agg["email_ready"]
    report_missing = agg["report_missing"]

    return {
        "month": month,
        "total": total,
        "dispatched": dispatched,
        "pending_dispatch": total - dispatched,
        "dispatch_ready": agg["dispatch_ready"],
        "emailed": agg["emailed"],
        "email_na": agg["email_na"],
        "email_ready": email_ready,
        "report_missing": report_missing,
        "pending_email": email_ready + report_missing,
        "completed": agg["completed"],
        "closed": agg["closed"],
        "unattributed": unattributed,
    }


@router.get("/api/statistics/by-region")
async def statistics_by_region(
    month: str | None = Query(None, description="YYYY-MM"),
):
    """月度区域分布。"""
    month = _normalize_month(month)
    records = await _load_dispatch_records()
    months, _ = _aggregate_by_month(records)
    agg = months.get(month, _empty_month())
    items = [
        {"region": region, "count": count}
        for region, count in agg["regions"].most_common()
    ]
    return {"month": month, "items": items}


@router.get("/api/statistics/by-status")
async def statistics_by_status(
    month: str | None = Query(None, description="YYYY-MM"),
):
    """月度派单状态 + 邮件状态（饼图口径，扇区合计等于工单总数）。"""
    month = _normalize_month(month)
    records = await _load_dispatch_records()
    months, _ = _aggregate_by_month(records)
    agg = months.get(month, _empty_month())

    total = agg["total"]
    dispatched = agg["dispatched"]
    dispatch_status = []
    if dispatched > 0:
        dispatch_status.append({"status": "已派单", "count": dispatched})
    if total - dispatched > 0:
        dispatch_status.append({"status": "待派单", "count": total - dispatched})

    email_status = []
    if agg["emailed"] > 0:
        email_status.append({"status": "已发送", "count": agg["emailed"]})
    if agg["email_ready"] > 0:
        email_status.append({"status": "待发送", "count": agg["email_ready"]})
    if agg["report_missing"] > 0:
        email_status.append({"status": "报告未上传", "count": agg["report_missing"]})
    if agg["email_na"] > 0:
        email_status.append({"status": "不涉及", "count": agg["email_na"]})

    return {
        "month": month,
        "dispatch_status": dispatch_status,
        "email_status": email_status,
    }


@router.get("/api/statistics/monthly-trend")
async def statistics_monthly_trend(
    month: str | None = Query(None, description="YYYY-MM, ends at the given month"),
):
    """近 6 个月（含所选月份）工单量与闭环量趋势。"""
    month = _normalize_month(month)
    records = await _load_dispatch_records()
    months, _ = _aggregate_by_month(records)

    items = []
    for m in _previous_months(month, 6):
        agg = months.get(m, _empty_month())
        items.append({
            "month": m,
            "total": agg["total"],
            "dispatched": agg["dispatched"],
            "emailed": agg["emailed"],
            "closed": agg["closed"],
        })
    return {"month": month, "items": items}


@router.get("/api/statistics/triggers")
async def statistics_triggers(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Trigger success/failure counts grouped by trigger_type.

    注意：该接口基于本地触发日志（TriggerLog），独立于钉钉表月度口径，
    用于观察自动化触发成功/失败情况。
    """
    month = _normalize_month(month)
    start, end = _month_range(month)

    rows = (
        db.query(
            TriggerLog.trigger_type,
            TriggerLog.status,
            func.count(TriggerLog.id),
        )
        .filter(
            TriggerLog.created_at >= datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
            TriggerLog.created_at <= datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc),
        )
        .group_by(TriggerLog.trigger_type, TriggerLog.status)
        .all()
    )
    return {
        "month": month,
        "items": [
            {"trigger_type": trigger_type, "status": status, "count": count}
            for trigger_type, status, count in rows
        ],
    }
