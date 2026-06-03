"""Trigger service: execute yunji dispatch and email sending.

Both triggers are idempotent: check trigger_logs for existing successful execution.

Yunji dispatch now uses direct API calls:
- PTS data: GraphQL API (pts_client.py)
- Yunji API: direct HTTP with session cookie (yunji_client.py + yunji_dispatch.py)
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models.trigger_log import TriggerLog
from models.work_order import WorkOrder

logger = logging.getLogger(__name__)


async def trigger_yunji_dispatch(db: Session, work_order_id: uuid.UUID) -> dict:
    """Trigger yunji dispatch for a work order.

    Idempotent: skip if a successful trigger_log already exists.
    """
    wo = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not wo:
        return {"status": "error", "message": "Work order not found"}

    pts_url = wo.pts_order_url or f"https://pts.chaitin.net/project/order/{wo.pts_order_id}"
    supplier = wo.partner_supplier

    result = await _call_yunji_dispatch(db, work_order_id, pts_url, supplier,
                                         trigger_reason=f"工程师={wo.engineer}, 伙伴服务商={wo.partner_supplier}")

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
    # Try to find a matching WorkOrder by customer_name for trigger_log
    wo = None
    if customer_name:
        wo = db.query(WorkOrder).filter(WorkOrder.customer_name == customer_name).first()

    work_order_id = wo.id if wo else None

    result = await _call_yunji_dispatch(
        db, work_order_id, pts_url, supplier,
        trigger_reason=f"AITable派单: 供应商={supplier}, 客户={customer_name}, record={record_id}",
        skip_idempotency=True,  # AITable records don't have prior trigger_logs
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
    max_retries: int = 2,
) -> dict:
    """Core yunji dispatch: direct API call with automatic retry.

    Retries up to max_retries times on transient errors (API returned null,
    network timeout). Permanent errors (session expired, data missing) are
    not retried.
    """
    # Idempotency check
    if not skip_idempotency and work_order_id:
        existing = db.query(TriggerLog).filter(
            TriggerLog.work_order_id == work_order_id,
            TriggerLog.trigger_type == "yunji_dispatch",
            TriggerLog.status == "success",
        ).first()
        if existing:
            return {"status": "skipped", "message": "Already dispatched successfully"}

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
        trigger_type="yunji_dispatch",
        trigger_reason=trigger_reason,
        status="pending",
    )
    db.add(log)
    db.commit()

    payload = {"ptsUrl": pts_url, "supplier": supplier}
    log.request_payload = payload

    last_error = None
    for attempt in range(1 + max_retries):
        try:
            from services.yunji_dispatch import create_yunji_requirement

            result = await create_yunji_requirement(pts_url, supplier, db=db)

            demand_id = result.get("demandId", "")
            order_id = result.get("orderId", "")

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
            # Session expired — permanent error, don't retry
            log.status = "failed"
            log.response_body = {"error": str(e)}
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.error("Yunji session expired: %s", e)
            return {"status": "failed", "message": str(e)}

        except Exception as e:
            last_error = e
            is_transient = _is_transient_error(e)
            if is_transient and attempt < max_retries:
                logger.warning(
                    "Dispatch attempt %d/%d failed (transient): %s, retrying in 3s...",
                    attempt + 1, 1 + max_retries, e,
                )
                await asyncio.sleep(3)
                continue
            # Permanent error or all retries exhausted
            log.status = "failed"
            log.response_body = {"error": str(e)}
            log.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.exception("Yunji dispatch failed after %d attempts", attempt + 1)
            return {"status": "failed", "message": str(e)}

    return {"status": "failed", "message": str(last_error)}


def _is_transient_error(e: Exception) -> bool:
    """Determine if an error is transient (worth retrying) vs permanent.

    Transient: API returned null, network timeout, rate limit, temporary 5xx
    Permanent: session expired, data not found (supplier/assigner/region)
    """
    msg = str(e)

    # Permanent patterns — definitely don't retry
    permanent_patterns = [
        "session",
        "已过期",
        "未配置",
        "未找到",
        "权限",
        "GraphQL 错误",
    ]
    for pattern in permanent_patterns:
        if pattern in msg:
            return False

    # Transient patterns
    transient_patterns = [
        "API返回空结果",
        "Connection",
        "timeout",
        "Timed out",
        "429",
        "502",
        "503",
        "504",
    ]
    for pattern in transient_patterns:
        if pattern in msg:
            return True

    # Default: treat unknown errors as transient (safer to retry once)
    return True


async def trigger_email_send(db: Session, work_order_id: uuid.UUID) -> dict:
    """Trigger email sending for a work order.

    Idempotent: skip if a successful trigger_log already exists.
    """
    wo = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not wo:
        return {"status": "error", "message": "Work order not found"}

    # Idempotency check
    existing = db.query(TriggerLog).filter(
        TriggerLog.work_order_id == work_order_id,
        TriggerLog.trigger_type == "inspection_email",
        TriggerLog.status == "success",
    ).first()
    if existing:
        return {"status": "skipped", "message": "Already sent successfully"}

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
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
    # Try to find a matching WorkOrder for trigger_log
    wo = None
    if customer_name:
        wo = db.query(WorkOrder).filter(WorkOrder.customer_name == customer_name).first()

    work_order_id = wo.id if wo else None

    # Create trigger log
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=work_order_id,
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