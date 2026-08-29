"""Trigger service: execute yunji dispatch and email sending.

Both triggers are idempotent: check trigger_logs for existing successful execution.

Yunji dispatch now uses direct API calls:
- PTS data: GraphQL API (pts_client.py)
- Yunji API: direct HTTP with session cookie (yunji_client.py + yunji_dispatch.py)
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models.trigger_log import TriggerLog
from models.work_order import WorkOrder
from models.work_order_sync import WorkOrderSync

logger = logging.getLogger(__name__)


async def trigger_yunji_dispatch(
    db: Session,
    work_order_id: uuid.UUID,
    *,
    record_id: str | None = None,
) -> dict:
    """Trigger yunji dispatch for a work order.

    Idempotent: skip if a successful trigger_log already exists.
    """
    wo = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not wo:
        return {"status": "error", "message": "Work order not found"}

    pts_url = wo.pts_order_url or f"https://pts.chaitin.net/project/order/{wo.pts_order_id}"
    supplier = wo.partner_supplier or ""

    result = await _call_yunji_dispatch(
        db,
        work_order_id,
        pts_url,
        supplier or "",
        trigger_reason=f"工程师={wo.engineer}, 伙伴服务商={wo.partner_supplier}",
        aitable_record_id=record_id,
    )

    if result.get("status") == "success":
        await _broadcast_trigger("trigger.dispatch.success", wo)
    else:
        await _broadcast_trigger("trigger.dispatch.failed", wo, error=result.get("message", ""))
    return result


async def dispatch_from_aitable(
    db: Session,
    *,
    pts_url: str,
    supplier: str,
    record_id: str,
    customer_name: str | None = None,
) -> dict:
    """Trigger yunji dispatch for an AITable record (客户巡检派单表).

    Does NOT require a local WorkOrder. Creates a trigger_log linked
    to a dummy work_order (or None if no match). Returns demandId and orderId.
    """
    # Resolve the exact monthly WorkOrderSync first; customer name is only a
    # compatibility fallback for records created before the association table.
    wo = db.query(WorkOrder).join(
        WorkOrderSync,
        WorkOrderSync.work_order_id == WorkOrder.id,
    ).filter(WorkOrderSync.aitable_record_id == record_id).first()
    if wo is None and customer_name:
        wo = db.query(WorkOrder).filter(WorkOrder.customer_name == customer_name).first()

    work_order_id = wo.id if wo else None

    result = await _call_yunji_dispatch(
        db, work_order_id, pts_url, supplier,
        trigger_reason=f"AITable派单: 供应商={supplier}, 客户={customer_name}, record={record_id}",
        aitable_record_id=record_id,
    )

    # Broadcast WebSocket event for frontend display
    if result.get("status") == "success":
        demand_id = result.get("demandId", "")
        order_id = result.get("orderId", "")
        if wo:
            await _broadcast_trigger("trigger.dispatch.success", wo)
        # Send DingTalk notification
        await _notify_dispatch_success(supplier, customer_name or "", demand_id, order_id, pts_url)
    elif result.get("status") == "failed":
        if wo:
            await _broadcast_trigger("trigger.dispatch.failed", wo, error=result.get("message", ""))
        # Send DingTalk notification
        await _notify_dispatch_failed(supplier, customer_name or "", result.get("message", ""))

    return result


async def _call_yunji_dispatch(
    db: Session,
    work_order_id: uuid.UUID | None,
    pts_url: str,
    supplier: str,
    *,
    trigger_reason: str,
    skip_idempotency: bool = False,
    aitable_record_id: str | None = None,
) -> dict:
    """Core yunji dispatch: direct API call (no Puppeteer).

    Flow:
    1. Fetch PTS data via GraphQL API
    2. Resolve delivery assigner → department → region leader
    3. Call yunji API directly with session cookie
    4. Return demandId and orderId

    Returns: {"status": "success"/"failed", "demandId": ..., "orderId": ..., ...}
    """
    # Idempotency check
    if not skip_idempotency:
        existing_query = db.query(TriggerLog).filter(
            TriggerLog.trigger_type == "yunji_dispatch",
            TriggerLog.status == "success",
        )
        if aitable_record_id:
            existing_query = existing_query.filter(
                TriggerLog.aitable_record_id == aitable_record_id,
            )
        else:
            existing_query = existing_query.filter(
                TriggerLog.work_order_id == work_order_id,
            )
        existing = existing_query.first()
        if existing:
            existing_body = existing.response_body or {}
            if existing_body.get("demandId"):
                return {"status": "skipped", "message": "Already dispatched successfully"}
            # 历史"假成功"日志（无需求 ID）不阻塞重试：标记为失败，由监控周期自动重试
            logger.warning(
                "发现无 demandId 的假成功派单日志，标记失败并允许重试: trigger_log=%s, aitable_record_id=%s",
                existing.id, aitable_record_id,
            )
            existing.status = "failed"
            existing.error_message = "历史成功日志无需求 ID，标记失败以触发重试"
            db.commit()

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
        aitable_record_id=aitable_record_id,
        trigger_type="yunji_dispatch",
        trigger_reason=trigger_reason,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    payload = {"ptsUrl": pts_url, "supplier": supplier}
    log.request_payload = payload

    try:
        from services.yunji_dispatch import create_yunji_requirement

        result = await create_yunji_requirement(pts_url, supplier, db=db)

        demand_id = result.get("demandId", "")
        order_id = result.get("orderId", "")
        if not demand_id:
            raise RuntimeError("云集派单返回空 demandId，无法确认需求已创建")

        log.status = "success"
        log.response_body = result
        log.completed_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "status": "success",
            "demandId": demand_id,
            "orderId": order_id,
        }

    except PermissionError as e:
        log.status = "failed"
        log.response_body = {"error": str(e)}
        log.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error("Yunji session expired: %s", e)
        return {"status": "failed", "message": str(e)}

    except Exception as e:
        log.status = "failed"
        log.response_body = {"error": str(e)}
        log.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.exception("Yunji dispatch failed")
        return {"status": "failed", "message": str(e)}


async def trigger_email_send(
    db: Session,
    work_order_id: uuid.UUID,
    *,
    record_id: str | None = None,
) -> dict:
    """Trigger email sending for a work order.

    Idempotent: skip if a successful trigger_log already exists.
    """
    wo = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not wo:
        return {"status": "error", "message": "Work order not found"}

    # Idempotency check
    existing_query = db.query(TriggerLog).filter(
        TriggerLog.trigger_type == "inspection_email",
        TriggerLog.status == "success",
    )
    if record_id:
        existing_query = existing_query.filter(TriggerLog.aitable_record_id == record_id)
    else:
        existing_query = existing_query.filter(TriggerLog.work_order_id == work_order_id)
    existing = existing_query.first()
    if existing:
        return {"status": "skipped", "message": "Already sent successfully"}

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
        aitable_record_id=record_id,
        trigger_type="inspection_email",
        trigger_reason=f"email_sent={wo.email_sent}, email_trigger_status={wo.email_trigger_status}",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    try:
        from services.email_sender import send_inspection_email
        result = send_inspection_email(
            customer_name=wo.customer_name or "",
            product_name=wo.product_name or "",
        )

        if result.get("success"):
            log.status = "success"
            log.response_body = result
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            await _broadcast_trigger("trigger.email.success", wo)
            return {"status": "success", "message": "Email sent"}
        else:
            log.status = "failed"
            log.response_body = result
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            await _broadcast_trigger("trigger.email.failed", wo, error=result.get("error", "Unknown error"))
            return {"status": "failed", "message": result.get("error", "Unknown error")}

    except Exception as e:
        log.status = "failed"
        log.response_body = {"error": str(e)}
        log.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.exception("Email send failed for %s", wo.pts_order_id)
        await _broadcast_trigger("trigger.email.failed", wo, error=str(e))
        return {"status": "failed", "message": str(e)}


async def email_from_aitable(
    db: Session,
    *,
    record_id: str,
    customer_name: str,
    product_name: str,
    email_addresses: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict:
    """Send inspection email for an AITable record (增值服务进度明细表).

    Does NOT require a local WorkOrder. Creates a trigger_log linked
    to a matching WorkOrder (or dummy UUID). Returns send result.
    """
    # Resolve by monthly association so a historical record cannot update the
    # current month's WorkOrder instance by customer name alone.
    wo = db.query(WorkOrder).join(
        WorkOrderSync,
        WorkOrderSync.work_order_id == WorkOrder.id,
    ).filter(WorkOrderSync.aitable_record_id == record_id).first()
    if wo is None and customer_name:
        wo = db.query(WorkOrder).filter(WorkOrder.customer_name == customer_name).first()

    work_order_id = wo.id if wo else None

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
        aitable_record_id=record_id,
        trigger_type="inspection_email",
        trigger_reason=f"AITable邮件: 客户={customer_name}, record={record_id}",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    try:
        from services.email_sender import send_inspection_email
        result = send_inspection_email(
            customer_name=customer_name,
            product_name=product_name,
            to_emails=email_addresses or [],
            attachments=attachments,
        )

        if result.get("success"):
            log.status = "success"
            log.response_body = result
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {"status": "success", "message": result.get("message", "邮件发送成功")}
        else:
            log.status = "failed"
            log.response_body = result
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {"status": "failed", "message": result.get("message", "邮件发送失败")}

    except Exception as e:
        log.status = "failed"
        log.response_body = {"error": str(e)}
        log.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.exception("Email from AITable failed for record %s", record_id)
        return {"status": "failed", "message": f"邮件发送异常: {e}"}


async def _broadcast_trigger(event_type: str, wo: WorkOrder, error: str | None = None) -> None:
    """Broadcast a trigger event via WebSocket."""
    try:
        from apps.api.routers.ws import broadcaster

        data = {
            "work_order_id": str(wo.id),
            "pts_order_id": wo.pts_order_id,
            "customer_name": wo.customer_name,
        }
        if error:
            data["error"] = error
        await broadcaster.broadcast(event_type, data)
    except Exception:
        logger.debug("WebSocket broadcast failed for %s", event_type)


async def _notify_dispatch_success(
    supplier: str,
    customer_name: str,
    demand_id: str,
    order_id: str,
    pts_url: str,
) -> None:
    """Send DingTalk notification for dispatch success."""
    try:
        from services.dingtalk_notifier import notify_dispatch_success
        await notify_dispatch_success(supplier, customer_name, demand_id, order_id, pts_url)
    except Exception as e:
        logger.debug("DingTalk notification failed for dispatch success: %s", e)


async def _notify_dispatch_failed(supplier: str, customer_name: str, error: str) -> None:
    """Send DingTalk notification for dispatch failure."""
    try:
        from services.dingtalk_notifier import notify_dispatch_failed
        await notify_dispatch_failed(supplier, customer_name, error)
    except Exception as e:
        logger.debug("DingTalk notification failed for dispatch failure: %s", e)
