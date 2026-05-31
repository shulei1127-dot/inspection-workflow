"""Statistics API: aggregation queries for the frontend dashboard.

Endpoints:
- GET /api/statistics/overview      — Monthly overview counts
- GET /api/statistics/by-region     — Work order count by region
- GET /api/statistics/by-type       — Work order count by type
- GET /api/statistics/by-status     — Work order count by status
- GET /api/statistics/monthly-trend — Daily work order count for the month
- GET /api/statistics/triggers      — Trigger success/failure stats
"""

from datetime import date, datetime, timezone
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import Session

from core.db import get_db
from models.sync_log import SyncLog
from models.trigger_log import TriggerLog
from models.work_order import WorkOrder
from services.aitable_fields import current_month

logger = logging.getLogger(__name__)

router = APIRouter(tags=["statistics"])


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
    db: Session = Depends(get_db),
):
    """Monthly overview: total, dispatched, emailed, pending counts.

    pending_dispatch and pending_email are fetched from AITable in real-time
    to stay consistent with the monitor page.
    """
    month = month or current_month()
    start, end = _month_range(month)

    q = db.query(WorkOrder).filter(
        WorkOrder.planned_completion >= start,
        WorkOrder.planned_completion <= end,
    )

    total = q.count()
    dispatched = q.filter(WorkOrder.dispatch_status == "已派单").count()
    dispatch_failed = q.filter(WorkOrder.dispatch_status == "派单失败").count()
    emailed = q.filter(WorkOrder.email_trigger_status == "已发送").count()
    email_failed = q.filter(WorkOrder.email_trigger_status == "发送失败").count()
    synced = q.filter(WorkOrder.dt_sync_status == "synced").count()
    sync_failed = q.filter(WorkOrder.dt_sync_status == "failed").count()

    # Fetch pending counts from AITable (consistent with monitor page)
    # Use count_only for speed and asyncio.gather for parallel execution
    pending_dispatch = 0
    pending_email = 0
    try:
        import asyncio
        from services.monitor_service import get_dispatch_pending, get_email_pending
        dispatch_result, email_result = await asyncio.gather(
            get_dispatch_pending(db, count_only=True),
            get_email_pending(db, count_only=True),
        )
        pending_dispatch = dispatch_result.get("total", 0)
        pending_email = email_result.get("total", 0)
    except Exception:
        logger.warning("AITable query failed in overview, falling back to local DB", exc_info=True)
        pending_dispatch = q.filter(WorkOrder.dispatch_status == "待派单").count()
        pending_email = q.filter(
            WorkOrder.email_trigger_status == "待发送",
            WorkOrder.email_sent == "否",
        ).count()

    return {
        "month": month,
        "total": total,
        "dispatched": dispatched,
        "dispatch_failed": dispatch_failed,
        "emailed": emailed,
        "email_failed": email_failed,
        "pending_dispatch": pending_dispatch,
        "pending_email": pending_email,
        "synced": synced,
        "sync_failed": sync_failed,
    }


@router.get("/api/statistics/by-region")
async def statistics_by_region(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Work order count grouped by region."""
    month = month or current_month()
    start, end = _month_range(month)

    rows = (
        db.query(WorkOrder.region, func.count(WorkOrder.id))
        .filter(
            WorkOrder.planned_completion >= start,
            WorkOrder.planned_completion <= end,
        )
        .group_by(WorkOrder.region)
        .all()
    )
    return {
        "month": month,
        "items": [
            {"region": region or "未分配", "count": count}
            for region, count in rows
        ],
    }


@router.get("/api/statistics/by-type")
async def statistics_by_type(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Work order count grouped by order_type."""
    month = month or current_month()
    start, end = _month_range(month)

    rows = (
        db.query(WorkOrder.order_type, func.count(WorkOrder.id))
        .filter(
            WorkOrder.planned_completion >= start,
            WorkOrder.planned_completion <= end,
        )
        .group_by(WorkOrder.order_type)
        .all()
    )
    return {
        "month": month,
        "items": [
            {"type": t or "未知", "count": count}
            for t, count in rows
        ],
    }


@router.get("/api/statistics/by-status")
async def statistics_by_status(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Work order count grouped by dispatch_status + email_trigger_status.

    Dispatch and email status include AITable real-time data.
    """
    month = month or current_month()
    start, end = _month_range(month)

    dispatch_rows = (
        db.query(WorkOrder.dispatch_status, func.count(WorkOrder.id))
        .filter(
            WorkOrder.planned_completion >= start,
            WorkOrder.planned_completion <= end,
        )
        .group_by(WorkOrder.dispatch_status)
        .all()
    )
    email_rows = (
        db.query(WorkOrder.email_trigger_status, func.count(WorkOrder.id))
        .filter(
            WorkOrder.planned_completion >= start,
            WorkOrder.planned_completion <= end,
        )
        .group_by(WorkOrder.email_trigger_status)
        .all()
    )

    dispatch_status = [
        {"status": s or "未知", "count": count}
        for s, count in dispatch_rows
    ]
    email_status = [
        {"status": s or "未知", "count": count}
        for s, count in email_rows
    ]

    # Merge AITable real-time pending counts (parallel + count_only for speed)
    try:
        import asyncio
        from services.monitor_service import get_dispatch_pending, get_email_pending
        dispatch_result, email_result = await asyncio.gather(
            get_dispatch_pending(db, count_only=True),
            get_email_pending(db, count_only=True),
        )
        aitable_pending_dispatch = dispatch_result.get("total", 0)
        aitable_pending_email = email_result.get("total", 0)

        # Update "待派单" count in dispatch_status with AITable value
        found = False
        for item in dispatch_status:
            if item["status"] == "待派单":
                item["count"] = aitable_pending_dispatch
                found = True
                break
        if not found and aitable_pending_dispatch > 0:
            dispatch_status.append({"status": "待派单", "count": aitable_pending_dispatch})

        # Update "待发送" count in email_status with AITable value
        found = False
        for item in email_status:
            if item["status"] == "待发送":
                item["count"] = aitable_pending_email
                found = True
                break
        if not found and aitable_pending_email > 0:
            email_status.append({"status": "待发送", "count": aitable_pending_email})
    except Exception:
        logger.warning("AITable query failed in by-status, using local DB counts", exc_info=True)

    return {
        "month": month,
        "dispatch_status": dispatch_status,
        "email_status": email_status,
    }


@router.get("/api/statistics/monthly-trend")
async def statistics_monthly_trend(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Daily work order count trend for the given month."""
    month = month or current_month()
    start, end = _month_range(month)

    rows = (
        db.query(
            cast(WorkOrder.created_at, Date).label("day"),
            func.count(WorkOrder.id),
        )
        .filter(
            WorkOrder.planned_completion >= start,
            WorkOrder.planned_completion <= end,
        )
        .group_by(cast(WorkOrder.created_at, Date))
        .order_by(cast(WorkOrder.created_at, Date))
        .all()
    )
    return {
        "month": month,
        "items": [
            {"date": day.isoformat(), "count": count}
            for day, count in rows
        ],
    }


@router.get("/api/statistics/triggers")
async def statistics_triggers(
    month: str | None = Query(None, description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Trigger success/failure counts grouped by trigger_type."""
    month = month or current_month()
    start, end = _month_range(month)

    rows = (
        db.query(
            TriggerLog.trigger_type,
            TriggerLog.status,
            func.count(TriggerLog.id),
        )
        .filter(
            TriggerLog.created_at >= start,
            TriggerLog.created_at <= end,
        )
        .group_by(TriggerLog.trigger_type, TriggerLog.status)
        .all()
    )
    return {
        "month": month,
        "items": [
            {
                "trigger_type": trigger_type,
                "status": status,
                "count": count,
            }
            for trigger_type, status, count in rows
        ],
    }
