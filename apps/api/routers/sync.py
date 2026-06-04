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
from services.sync_service import run_sync, push_to_aitable, _sync_to_aitable, _build_aitable_url_map, current_month
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

    # Auto-adjust planned completion to month end
    from services.sync_service import adjust_planned_completion_to_month_end
    adjust_result = adjust_planned_completion_to_month_end(db, month=log.sync_month)

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
    """Batch push selected work orders by ID list to DingTalk AITable."""
    pushed = 0
    failed = 0
    sync_month = current_month()

    # Build AITable dedup map once
    aitable_url_map = await _build_aitable_url_map()
    if aitable_url_map is None:
        return {
            "status": "error",
            "pushed": 0,
            "failed": 0,
            "total": len(req.work_order_ids),
            "message": "AITable 不可达，已中止推送以防止重复记录",
        }

    for wo_id_str in req.work_order_ids:
        try:
            wo_id = uuid.UUID(wo_id_str)
            wo = db.query(WorkOrder).filter(WorkOrder.id == wo_id).first()
            if wo:
                # Reset dt_record_id to force re-sync
                wo.dt_record_id = None
                wo.dt_sync_status = "pending"
                await _sync_to_aitable(db, wo, sync_month=sync_month, aitable_url_map=aitable_url_map)
                # Check if sync was actually successful
                if wo.dt_sync_status == "synced" and wo.dt_record_id:
                    pushed += 1
                else:
                    logger.warning(f"Work order {wo.pts_order_id} sync status: {wo.dt_sync_status}, record_id: {wo.dt_record_id}")
                    failed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"Failed to push work order {wo_id_str}: {e}")
            failed += 1

    db.commit()

    return {
        "status": "success" if failed == 0 else "partial",
        "pushed": pushed,
        "failed": failed,
        "total": len(req.work_order_ids),
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
