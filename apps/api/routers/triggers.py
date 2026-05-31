"""Trigger endpoints: manually trigger yunji dispatch or email sending."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from services.trigger_service import trigger_email_send, trigger_yunji_dispatch
from apps.api.utils import fmt_cst

router = APIRouter(tags=["triggers"])


@router.post("/api/triggers/yunji/{work_order_id}")
async def manual_yunji_dispatch(
    work_order_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Manually trigger yunji dispatch for a work order."""
    result = await trigger_yunji_dispatch(db, work_order_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/api/triggers/email/{work_order_id}")
async def manual_email_send(
    work_order_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Manually trigger email sending for a work order."""
    result = await trigger_email_send(db, work_order_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/api/triggers/logs")
async def get_trigger_logs(
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """List recent trigger logs."""
    from models.trigger_log import TriggerLog
    logs = db.query(TriggerLog).order_by(TriggerLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id": str(log.id),
            "work_order_id": str(log.work_order_id),
            "trigger_type": log.trigger_type,
            "trigger_reason": log.trigger_reason,
            "status": log.status,
            "response_status": log.response_status,
            "retry_count": log.retry_count,
            "created_at": fmt_cst(log.created_at),
            "completed_at": fmt_cst(log.completed_at),
        }
        for log in logs
    ]
