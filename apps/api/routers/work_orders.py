"""Work order endpoints: list and query work orders."""

import calendar
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from models.work_order import WorkOrder
from apps.api.utils import fmt_cst

router = APIRouter(tags=["work-orders"])


@router.get("/api/work-orders")
async def list_work_orders(
    month: str | None = Query(None, description="按月份筛选，格式 YYYY-MM"),
    order_type: str | None = Query(None, description="按工单类型筛选"),
    status: str | None = Query(None, description="按状态筛选"),
    dispatch_status: str | None = Query(None, description="按派单状态筛选"),
    dt_sync_status: str | None = Query(None, description="按钉钉推送状态筛选"),
    closure_status: str | None = Query(None, description="按闭环状态筛选：未闭环/已闭环/闭环中/闭环失败"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List work orders with optional filters."""
    q = db.query(WorkOrder).filter(~WorkOrder.pts_order_id.startswith("dt_"))

    if month:
        parts = month.split("-")
        year, m = int(parts[0]), int(parts[1])
        start_date = date(year, m, 1)
        last_day = calendar.monthrange(year, m)[1]
        end_date = date(year, m, last_day)
        q = q.filter(WorkOrder.planned_completion >= start_date)
        q = q.filter(WorkOrder.planned_completion <= end_date)

    if order_type:
        q = q.filter(WorkOrder.order_type == order_type)
    if status:
        q = q.filter(WorkOrder.status == status)
    if dispatch_status:
        q = q.filter(WorkOrder.dispatch_status == dispatch_status)
    if dt_sync_status:
        q = q.filter(WorkOrder.dt_sync_status == dt_sync_status)
    if closure_status:
        q = q.filter(WorkOrder.closure_status == closure_status)

    total = q.count()
    orders = q.order_by(WorkOrder.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "items": [_serialize_work_order(wo) for wo in orders],
    }


@router.get("/api/work-orders/pending")
async def list_pending_work_orders(
    db: Session = Depends(get_db),
):
    """List work orders that are pending trigger actions."""
    pending_dispatch = db.query(WorkOrder).filter(
        WorkOrder.dispatch_status == "待派单",
    ).all()

    pending_email = db.query(WorkOrder).filter(
        WorkOrder.email_trigger_status == "待发送",
        WorkOrder.email_sent == "否",
    ).all()

    return {
        "pending_dispatch": [_serialize_work_order(wo) for wo in pending_dispatch],
        "pending_email": [_serialize_work_order(wo) for wo in pending_email],
        "pending_dispatch_count": len(pending_dispatch),
        "pending_email_count": len(pending_email),
    }


def _serialize_work_order(wo: WorkOrder) -> dict:
    return {
        "id": str(wo.id),
        "pts_order_id": wo.pts_order_id,
        "pts_order_url": wo.pts_order_url,
        "order_type": wo.order_type,
        "customer_name": wo.customer_name,
        "product_name": wo.product_name,
        "engineer": wo.engineer,
        "region": wo.region,
        "assigner_name": wo.assigner_name,
        "partner_supplier": wo.partner_supplier,
        "planned_completion": wo.planned_completion.isoformat() if wo.planned_completion else None,
        "status": wo.status,
        "email_sent": wo.email_sent,
        "dt_sync_status": wo.dt_sync_status,
        "dispatch_status": wo.dispatch_status,
        "email_trigger_status": wo.email_trigger_status,
        "closure_status": wo.closure_status,
        "created_at": fmt_cst(wo.created_at),
        "updated_at": fmt_cst(wo.updated_at),
    }
