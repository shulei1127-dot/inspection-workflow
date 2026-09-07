"""Statistics API: aggregation queries for the frontend dashboard.

数据概览以钉钉 AI 数据表《客户巡检派单》为唯一事实源，按记录时间按「月」或
「年」归集。本地 work_orders.planned_completion 只反映工单当前计划完成日期，
无法还原钉钉表每月新增的历史行，因此不用于统计。

业务口径（2026-09 确认，基于钉钉表字段）：
- 工单总数   = 所选范围内记录行数
- 已派单     = 需求单号非空且去重（一次派单覆盖多个巡检工单时，需求单号相同只算 1 次）
- 待派单     = 巡检方式=现场 且 需求单号为空 的数量
              + ceil(巡检方式=远程 且 需求单号为空 的数量 / 3)
              （远程 3 次巡检共用 1 个需求单号；远程-舒磊/延期巡检不涉及派单，不计入）
- 已发邮件   = 邮件是否发送 = 是
- 待发邮件   = 邮件是否发送 = 否（不涉及/未填写不计入）
- 不涉及邮件 = 邮件是否发送 = 不涉及（客户不发邮件，巡检报告可为空）
- 已闭环     = 工单是否闭环 = 是

Endpoints:
- GET /api/statistics/overview      — 月/年核心指标（month 或 year 二选一）
- GET /api/statistics/by-region     — 月/年区域分布（按记录行数）
- GET /api/statistics/by-status     — 月/年派单/邮件状态（饼图口径）
- GET /api/statistics/monthly-trend — 按月近 6 个月 / 按年 1-12 月（按记录行数）
- GET /api/statistics/triggers      — 触发日志成功/失败统计（本地库，独立口径）
"""

import asyncio
import logging
import math
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
    extract_select_name,
    extract_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["statistics"])

# 仅拉取概览需要的字段，降低 dws 查询耗时与传输量
_STATS_FIELD_IDS = ",".join([
    DISPATCH["记录时间"],
    DISPATCH["所属区域"],
    DISPATCH["巡检方式"],
    DISPATCH["需求编号"],
    DISPATCH["邮件是否发送"],
    DISPATCH["巡检是否完成"],
    DISPATCH["工单是否闭环"],
])

_FETCH_TTL_SECONDS = 30  # 同一份钉钉表数据在窗口期内复用，避免频繁调用 dws

_fetch_cache: dict = {"ts": 0.0, "records": []}
_fetch_lock: asyncio.Lock | None = None

_METHOD_ONSITE = "现场"
_METHOD_REMOTE = "远程"
_REMOTE_SHARE = 3  # 远程巡检 3 条记录共用 1 个需求单号


def _normalize_month(month: str | None) -> str:
    month = month or current_month()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(status_code=400, detail="month 参数格式须为 YYYY-MM")
    return month


async def _load_dispatch_records() -> list[dict]:
    """Fetch all 客户巡检派单 records once per TTL (single-flight)."""
    global _fetch_cache, _fetch_lock
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


def _resolve_scope(month: str | None, year: str | None) -> tuple[str, str]:
    """返回 (scope, value)；scope ∈ {month, year}，value 为 YYYY-MM 或 YYYY。"""
    if month is not None:
        return "month", _normalize_month(month)
    if year is not None:
        if not re.fullmatch(r"\d{4}", year):
            raise HTTPException(status_code=400, detail="year 参数格式须为 YYYY")
        return "year", year
    return "month", _normalize_month(None)


def _in_scope(record_month: str, scope: str, value: str) -> bool:
    if not record_month:
        return False
    if scope == "month":
        return record_month == value
    return record_month.startswith(value + "-")


def _empty_scope() -> dict:
    return {
        "total": 0,
        "demand_set": set(),
        "onsite_pending": 0,
        "remote_pending_rows": 0,
        "emailed": 0,
        "email_pending": 0,
        "email_na": 0,
        "email_unmarked": 0,
        "completed": 0,
        "closed": 0,
        "regions": Counter(),
    }


def _compute_scope(records: list[dict], scope: str, value: str) -> tuple[dict, int]:
    """按范围（月/年）统计一次，返回 (统计结果, 记录时间缺失条数)。

    统计口径见模块顶部注释。
    """
    acc = _empty_scope()
    unattributed = 0
    for record in records:
        cells = _cells_of(record)
        record_month = _record_month(cells)
        if not _in_scope(record_month, scope, value):
            if not record_month:
                unattributed += 1
            continue
        acc["total"] += 1
        region = extract_select_name(cells.get(DISPATCH["所属区域"])) or "未分配"
        acc["regions"][region] += 1

        demand = (extract_text(cells.get(DISPATCH["需求编号"])) or "").strip()
        method = extract_select_name(cells.get(DISPATCH["巡检方式"])) or ""
        if demand:
            acc["demand_set"].add(demand)
        elif method == _METHOD_ONSITE:
            acc["onsite_pending"] += 1
        elif method == _METHOD_REMOTE:
            acc["remote_pending_rows"] += 1
        # 远程-舒磊 / 延期巡检 / 巡检方式未填写：不涉及派单，不计待派单

        email = extract_select_name(cells.get(DISPATCH["邮件是否发送"]))
        if email == "是":
            acc["emailed"] += 1
        elif email == "否":
            acc["email_pending"] += 1
        elif email == "不涉及":
            acc["email_na"] += 1
        else:
            acc["email_unmarked"] += 1

        if extract_select_name(cells.get(DISPATCH["巡检是否完成"])) == "是":
            acc["completed"] += 1
        if extract_select_name(cells.get(DISPATCH["工单是否闭环"])) == "是":
            acc["closed"] += 1
    return acc, unattributed


def _scope_overview(acc: dict) -> dict:
    """把原始统计结果整理成接口输出结构。"""
    remote_rows = acc["remote_pending_rows"]
    remote_pending = math.ceil(remote_rows / _REMOTE_SHARE)
    pending_dispatch = acc["onsite_pending"] + remote_pending
    return {
        "total": acc["total"],
        "dispatched": len(acc["demand_set"]),
        "pending_dispatch": pending_dispatch,
        "pending_onsite": acc["onsite_pending"],
        "pending_remote_rows": remote_rows,
        "pending_remote": remote_pending,
        "emailed": acc["emailed"],
        "email_pending": acc["email_pending"],
        "email_na": acc["email_na"],
        "email_unmarked": acc["email_unmarked"],
        "completed": acc["completed"],
        "closed": acc["closed"],
    }


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


def _trend_months(scope: str, value: str) -> list[str]:
    """按月：近 6 个月；按年：该年 1-12 月。"""
    if scope == "year":
        return [f"{value}-{m:02d}" for m in range(1, 13)]
    return _previous_months(value, 6)


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
    month: str | None = Query(None, description="YYYY-MM, 与 year 二选一，默认当前月"),
    year: str | None = Query(None, description="YYYY, 全年口径"),
):
    """核心指标：按所选月份或年份统计（来源：钉钉客户巡检派单表）。"""
    scope, value = _resolve_scope(month, year)
    records = await _load_dispatch_records()
    acc, unattributed = _compute_scope(records, scope, value)
    overview = _scope_overview(acc)
    return {
        "scope": scope,
        "month": value if scope == "month" else None,
        "year": value if scope == "year" else None,
        **overview,
        "unattributed": unattributed,
    }


@router.get("/api/statistics/by-region")
async def statistics_by_region(
    month: str | None = Query(None, description="YYYY-MM, 与 year 二选一"),
    year: str | None = Query(None, description="YYYY, 全年口径"),
):
    """区域分布：按所选范围记录行数统计。"""
    scope, value = _resolve_scope(month, year)
    records = await _load_dispatch_records()
    acc, _ = _compute_scope(records, scope, value)
    items = [
        {"region": region, "count": count}
        for region, count in acc["regions"].most_common()
    ]
    return {"scope": scope, "month": value if scope == "month" else None,
            "year": value if scope == "year" else None, "items": items}


@router.get("/api/statistics/by-status")
async def statistics_by_status(
    month: str | None = Query(None, description="YYYY-MM, 与 year 二选一"),
    year: str | None = Query(None, description="YYYY, 全年口径"),
):
    """派单状态 + 邮件状态（饼图口径）。

    - 派单状态：已派单=需求单号去重次数；待派单=现场未派 + 远程未派折算
    - 邮件状态：已发送/待发送(=否)/不涉及/未填写
    """
    scope, value = _resolve_scope(month, year)
    records = await _load_dispatch_records()
    acc, _ = _compute_scope(records, scope, value)
    overview = _scope_overview(acc)

    dispatch_status = []
    if overview["dispatched"] > 0:
        dispatch_status.append({"status": "已派单", "count": overview["dispatched"]})
    if overview["pending_dispatch"] > 0:
        dispatch_status.append({"status": "待派单", "count": overview["pending_dispatch"]})

    email_status = []
    if overview["emailed"] > 0:
        email_status.append({"status": "已发送", "count": overview["emailed"]})
    if overview["email_pending"] > 0:
        email_status.append({"status": "待发送", "count": overview["email_pending"]})
    if overview["email_na"] > 0:
        email_status.append({"status": "不涉及", "count": overview["email_na"]})
    if overview["email_unmarked"] > 0:
        email_status.append({"status": "未填写", "count": overview["email_unmarked"]})

    return {"scope": scope, "month": value if scope == "month" else None,
            "year": value if scope == "year" else None,
            "dispatch_status": dispatch_status, "email_status": email_status}


@router.get("/api/statistics/monthly-trend")
async def statistics_monthly_trend(
    month: str | None = Query(None, description="YYYY-MM, 与 year 二选一"),
    year: str | None = Query(None, description="YYYY, 全年口径（返回该年 1-12 月）"),
):
    """趋势：按月=近 6 个月；按年=所选年 1-12 月（按记录行数）。"""
    scope, value = _resolve_scope(month, year)
    records = await _load_dispatch_records()

    # 逐月统计（行数口径，用于趋势图：工单量 / 已闭环）
    month_stats: dict[str, dict] = {}
    for record in records:
        cells = _cells_of(record)
        record_month = _record_month(cells)
        if not record_month:
            continue
        acc = month_stats.setdefault(record_month, _empty_scope())
        acc["total"] += 1
        if extract_select_name(cells.get(DISPATCH["工单是否闭环"])) == "是":
            acc["closed"] += 1
        if extract_select_name(cells.get(DISPATCH["邮件是否发送"])) == "是":
            acc["emailed"] += 1

    items = []
    for m in _trend_months(scope, value):
        acc = month_stats.get(m, _empty_scope())
        items.append({
            "month": m,
            "total": acc["total"],
            "emailed": acc["emailed"],
            "closed": acc["closed"],
        })
    return {"scope": scope, "month": value if scope == "month" else None,
            "year": value if scope == "year" else None, "items": items}


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
