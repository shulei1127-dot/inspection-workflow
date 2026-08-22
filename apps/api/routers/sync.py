"""Sync endpoints: trigger PTS→DingTalk sync, view sync logs."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from models.sync_log import SyncLog
from models.work_order import WorkOrder
from models.work_order_sync import WorkOrderSync
from services.sync_service import (
    run_sync,
    push_to_aitable,
    _sync_to_aitable,
    _build_dispatch_aitable_lookup,
    _acquire_dispatch_write_lock,
    _prepare_monthly_candidate,
    current_month,
)
from apps.api.utils import fmt_cst

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sync"])


class BatchPushRequest(BaseModel):
    work_order_ids: list[str]


@router.post("/api/sync/run")
async def trigger_sync(
    sync_month: str | None = Query(None, description="同步月份，格式 YYYY-MM，默认当月"),
    push: bool = Query(True, description="是否同时推送到钉钉AITable"),
    db: Session = Depends(get_db),
):
    """Pull PTS work orders to local DB and optionally push to DingTalk AITable."""
    log = await run_sync(db, trigger_source="manual", sync_month=sync_month, push_to_aitable=push)

    # Planned-completion write-back is an explicit operation at
    # /api/work-orders/adjust-planned-completion; a normal sync must remain
    # local-only for existing work orders.
    adjust_result = {"status": "not_run", "message": "请使用独立的计划完成时间调整接口"}

    return {
        "status": log.status,
        "sync_month": log.sync_month,
        "fetched_count": log.fetched_count,
        "created_count": log.created_count,
        "updated_count": log.updated_count,
        "skipped_count": log.skipped_count,
        "error_message": log.error_message,
        "adjust_result": adjust_result,
    }


@router.post("/api/sync/push")
async def push_to_dingtalk(
    sync_month: str | None = Query(None, description="推送月份，格式 YYYY-MM，默认当月"),
    db: Session = Depends(get_db),
):
    """Push pending work orders from local DB to DingTalk AITable."""
    return await push_to_aitable(db, sync_month=sync_month)


@router.post("/api/sync/batch-push")
async def batch_push_to_dingtalk(
    req: BatchPushRequest,
    db: Session = Depends(get_db),
):
    """Retry AITable creation only for explicitly eligible new work orders."""
    pushed = 0
    failed = 0
    skipped = 0
    items: list[dict] = []
    seen_ids: set[str] = set()
    seen_pts_ids: set[str] = set()
    candidates: list[tuple[WorkOrder, WorkOrderSync]] = []
    sync_month = current_month()

    for wo_id_str in req.work_order_ids:
        if wo_id_str in seen_ids:
            skipped += 1
            items.append({"work_order_id": wo_id_str, "status": "skipped", "reason": "duplicate request"})
            continue
        seen_ids.add(wo_id_str)

        try:
            wo_id = uuid.UUID(wo_id_str)
        except ValueError:
            failed += 1
            items.append({"work_order_id": wo_id_str, "status": "failed", "reason": "invalid work order id"})
            continue

        wo = db.query(WorkOrder).filter(WorkOrder.id == wo_id).first()
        if not wo:
            failed += 1
            items.append({"work_order_id": wo_id_str, "status": "failed", "reason": "work order not found"})
            continue

        if not wo.dt_create_eligible:
            skipped += 1
            items.append({
                "work_order_id": wo_id_str,
                "pts_order_id": wo.pts_order_id,
                "status": "skipped",
                "reason": "not an eligible new work order",
            })
            continue

        if wo.pts_order_id in seen_pts_ids:
            skipped += 1
            items.append({
                "work_order_id": wo_id_str,
                "pts_order_id": wo.pts_order_id,
                "status": "skipped",
                "reason": "duplicate PTS order in request",
            })
            continue

        seen_pts_ids.add(wo.pts_order_id)
        association = _prepare_monthly_candidate(db, wo, sync_month)
        if association is None:
            skipped += 1
            items.append({
                "work_order_id": wo_id_str,
                "pts_order_id": wo.pts_order_id,
                "status": "skipped",
                "reason": "target month already synced",
            })
            continue
        candidates.append((wo, association))

    if candidates:
        try:
            _acquire_dispatch_write_lock(db)
            aitable_lookup = await _build_dispatch_aitable_lookup()
        except Exception as e:
            logger.error("AITable dedup query failed; aborting batch push: %s", e)
            for wo, _association in candidates:
                failed += 1
                items.append({
                    "work_order_id": str(wo.id),
                    "pts_order_id": wo.pts_order_id,
                    "status": "failed",
                    "reason": "AITable dedup query failed",
                })
        else:
            for wo, association in candidates:
                try:
                    wo.dt_sync_status = "pending"
                    await _sync_to_aitable(
                        db,
                        wo,
                        sync_month=sync_month,
                        aitable_lookup=aitable_lookup,
                        sync_association=association,
                    )
                    if wo.dt_sync_status == "synced" and wo.dt_record_id:
                        pushed += 1
                        item_status = "synced"
                    else:
                        failed += 1
                        item_status = "failed"
                    items.append({
                        "work_order_id": str(wo.id),
                        "pts_order_id": wo.pts_order_id,
                        "status": item_status,
                    })
                except Exception as e:
                    logger.error("Failed to push work order %s: %s", wo.pts_order_id, e)
                    wo.dt_sync_status = "failed"
                    failed += 1
                    items.append({
                        "work_order_id": str(wo.id),
                        "pts_order_id": wo.pts_order_id,
                        "status": "failed",
                        "reason": str(e)[:200],
                    })

    db.commit()

    return {
        "status": "success" if failed == 0 else "partial",
        "pushed": pushed,
        "failed": failed,
        "skipped": skipped,
        "total": len(req.work_order_ids),
        "items": items,
    }


@router.get("/api/sync/logs")
async def get_sync_logs(
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
):
    """List recent sync logs."""
    logs = db.query(SyncLog).order_by(SyncLog.started_at.desc()).limit(limit).all()
    return [
        {
            "id": str(log.id),
            "trigger_source": log.trigger_source,
            "sync_month": log.sync_month,
            "status": log.status,
            "fetched_count": log.fetched_count,
            "created_count": log.created_count,
            "updated_count": log.updated_count,
            "skipped_count": log.skipped_count,
            "error_message": log.error_message,
            "started_at": fmt_cst(log.started_at),
            "completed_at": fmt_cst(log.completed_at),
        }
        for log in logs
    ]
