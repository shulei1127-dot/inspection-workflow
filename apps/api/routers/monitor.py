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


@router.post("/api/monitor/upload-report/{record_id}")
async def upload_report_to_pts(record_id: str, db: Session = Depends(get_db)):
    """Manually upload inspection reports from AITable to PTS for a specific record.

    Downloads report attachments from AITable, uploads them to PTS via internal API,
    and attaches the file IDs to the work order info note.

    Does NOT advance the work order stage or change closure status.
    """
    from core.config import get_settings
    from services.aitable_fields import DISPATCH, extract_text
    from services import dingtalk_client, pts_client
    from services.pts_closure_service import _extract_pts_order_id

    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"success": False, "message": "AITable 未配置"}

    # 1. Fetch AITable record
    records = await dingtalk_client.query_records(
        limit=100,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
    )

    target_record = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target_record = record
            break

    if not target_record:
        return {"success": False, "message": f"AITable 记录 {record_id} 未找到"}

    cells = target_record.get("cells", {})
    customer_name = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    report_attachments = cells.get(DISPATCH["巡检报告"])

    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        return {"success": False, "message": "巡检报告为空，无法上传"}

    # 2. Extract PTS order ID from link field
    link_val = cells.get(DISPATCH["巡检工单链接"])
    pts_order_id = _extract_pts_order_id(link_val)

    if not pts_order_id:
        return {"success": False, "message": "未找到 PTS 工单链接，无法上传"}

    if settings.inspection_closure_v2_enabled:
        from services.inspection_closure_v2 import coordinate_record

        result = await coordinate_record(
            db,
            target_record,
            source="manual_upload",
            reports_only=True,
        )
        return {
            "success": result.get("status") in {"report_ready", "completed"},
            "message": result.get("message") or result.get("status", "unknown"),
            "pts_order_id": pts_order_id,
            "customer_name": customer_name,
            "closure": result,
        }

    # 3. Download and upload reports
    file_ids = await pts_client.download_and_upload_reports(report_attachments)

    if not file_ids:
        return {
            "success": False,
            "message": "所有报告上传失败",
            "pts_order_id": pts_order_id,
            "customer_name": customer_name,
        }

    # 4. Add note with Markdown download links to PTS work order
    # PTS web UI renders [filename](/f/{file_id}) as clickable download links.
    note_text = f"巡检报告已上传（{len(file_ids)}个附件）"
    for att, fid in zip(report_attachments, file_ids):
        if isinstance(att, dict):
            filename = att.get("filename", "巡检报告")
            note_text += f"\n[{filename}](/f/{fid})"

    try:
        result = await pts_client.add_work_order_info(
            work_order_id=pts_order_id,
            note=note_text,
            file_ids=None,  # Markdown links in note text are the correct way for PTS web UI; file field doesn't render as clickable downloads
        )
        return {
            "success": True,
            "message": f"上传成功: {len(file_ids)}个文件已关联到工单 {pts_order_id}",
            "pts_order_id": pts_order_id,
            "customer_name": customer_name,
            "file_ids": file_ids,
            "note_added": result,
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"文件已上传但备注添加失败: {e}",
            "pts_order_id": pts_order_id,
            "customer_name": customer_name,
            "file_ids": file_ids,
        }
