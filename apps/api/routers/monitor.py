"""Monitor endpoints: trigger DingTalk AITable polls."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from services.monitor_service import (
    get_dispatch_pending,
    get_email_pending,
    invalidate_all_caches,
    run_closure_check,
    run_dispatch_monitor_poll,
    run_monitor_poll,
    trigger_manual_dispatch,
    trigger_manual_email,
)
from services.pts_closure_service import sync_closure_status_from_pts

router = APIRouter(tags=["monitor"])


@router.post("/api/monitor/poll")
async def trigger_poll(db: Session = Depends(get_db)):
    """Manually trigger a monitoring poll cycle (日常增值服务进展)."""
    result = await run_monitor_poll(db)
    return result


@router.post("/api/monitor/poll-dispatch")
async def trigger_dispatch_poll(db: Session = Depends(get_db)):
    """Manually trigger a dispatch monitor poll cycle (客户巡检派单)."""
    result = await run_dispatch_monitor_poll(db)
    return result


@router.get("/api/monitor/dispatch-pending")
async def list_dispatch_pending(
    refresh: bool = Query(False, description="Force refresh from AITable, bypass cache"),
    db: Session = Depends(get_db),
):
    """List AITable records that meet dispatch conditions."""
    if refresh:
        invalidate_all_caches()
    return await get_dispatch_pending(db)


@router.post("/api/monitor/dispatch/{record_id}")
async def manual_dispatch(record_id: str, db: Session = Depends(get_db)):
    """Manually trigger yunji dispatch for a specific AITable record."""
    return await trigger_manual_dispatch(db, record_id)


@router.get("/api/monitor/email-pending")
async def list_email_pending(
    refresh: bool = Query(False, description="Force refresh from AITable, bypass cache"),
    db: Session = Depends(get_db),
):
    """List AITable records that meet email sending conditions."""
    if refresh:
        invalidate_all_caches()
    return await get_email_pending(db)


@router.post("/api/monitor/send-email/{record_id}")
async def manual_send_email(
    record_id: str,
    emails: str | None = None,
    db: Session = Depends(get_db),
):
    """Manually trigger email sending for a specific AITable record.

    Optional `emails` query param: comma-separated recipient addresses.
    If provided, overrides the AITable 客户邮箱 field.
    """
    extra_emails = None
    if emails:
        extra_emails = [e.strip() for e in emails.replace("，", ",").split(",") if e.strip() and "@" in e]
    return await trigger_manual_email(db, record_id, extra_emails=extra_emails)


@router.get("/api/monitor/email-tool-url")
async def get_email_tool_url():
    """Return the URL of the Streamlit email tool."""
    from core.config import get_settings
    settings = get_settings()
    return {"url": f"http://localhost:{settings.email_tool_port}"}


@router.post("/api/monitor/closure-check")
async def trigger_closure_check(db: Session = Depends(get_db)):
    """Manually trigger a PTS work order closure check."""
    return await run_closure_check(db)


@router.post("/api/monitor/sync-closure-status")
async def trigger_sync_closure_status(db: Session = Depends(get_db)):
    """Sync closure status from PTS for all locally unclosed work orders."""
    return await sync_closure_status_from_pts(db)
