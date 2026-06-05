"""PTS work order closure service.

Automatically close work orders in PTS when inspection is complete:
1. Query unclosed work orders from local DB
2. Match with AITable 增值服务进度明细 records (via pts_order_id)
3. Check conditions: 巡检是否完成='是'
4. Assign work order to 舒磊
5. Add note to PTS work order referencing inspection report
6. Advance work order stage (confirm_work_order_stage) until 审核工单
7. Update local closure_status = "已闭环"
8. If permission error or other issues, mark as "需人工处理"

"""

import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from core.config import get_settings
from models.trigger_log import TriggerLog
from models.work_order import WorkOrder
from services import pts_client
from services.aitable_fields import DISPATCH, extract_select_name
from services import dingtalk_client

logger = logging.getLogger(__name__)

# Stage names that indicate the work order is already closed in PTS.
# 审核工单 is the target stage for auto-closure (not 已闭环)
_CLOSURE_STAGE_NAMES = {"审核工单", "已闭环", "结束"}

# Target stage for auto-closure (must advance to this stage)
_TARGET_CLOSURE_STAGE = "审核工单"

# Default assignee for auto-closure (舒磊 / lei.shu)
_DEFAULT_ASSIGNEE_ID = "669723ae2f6e1a862a49ef16"

# Max stage confirm attempts to prevent infinite loops
_MAX_STAGE_CONFIRM_ATTEMPTS = 10


async def sync_closure_status_from_pts(db: Session) -> dict:
    """Sync closure status from PTS for all locally unclosed work orders.

    For each work order where closure_status != "已闭环", query PTS to check
    if is_finished=true or current_stage indicates closure, and update the
    local closure_status accordingly.

    PTS rate limit is 4 req/s, so queries are naturally spaced by pts_client.
    """
    # Only query PTS for work orders that have a valid PTS order ID
    # (skip dt_ prefixed IDs which are AITable-only records without PTS counterparts)
    unclosed_orders = [
        wo for wo in db.query(WorkOrder).filter(
            WorkOrder.closure_status != "已闭环",
        ).all()
        if wo.pts_order_id and not wo.pts_order_id.startswith("dt_")
    ]
    skipped_count = 0
    all_unclosed = db.query(WorkOrder).filter(WorkOrder.closure_status != "已闭环").count()
    skipped_count = all_unclosed - len(unclosed_orders)

    if not unclosed_orders:
        return {"status": "success", "checked": 0, "updated": 0, "failed": 0, "skipped": skipped_count}

    logger.info("Sync closure status: checking %d unclosed PTS work orders (%d AITable-only skipped)", len(unclosed_orders), skipped_count)

    updated_count = 0
    failed_count = 0

    for wo in unclosed_orders:
        try:
            pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
            if pts_status is None:
                logger.warning("PTS returned no data for work order %s", wo.pts_order_id)
                failed_count += 1
                continue

            is_finished = pts_status.get("is_finished", False)
            current_stage = pts_status.get("current_stage") or {}
            stage_name = current_stage.get("name", "")

            if is_finished or stage_name in _CLOSURE_STAGE_NAMES:
                logger.info(
                    "Work order %s (%s) is already closed in PTS (is_finished=%s, stage=%s), updating local status",
                    wo.pts_order_id, wo.customer_name, is_finished, stage_name,
                )
                wo.closure_status = "已闭环"
                updated_count += 1
                db.commit()
        except Exception as e:
            logger.error("Failed to query PTS status for work order %s: %s", wo.pts_order_id, e)
            failed_count += 1

    result = {
        "status": "success",
        "checked": len(unclosed_orders),
        "updated": updated_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }
    logger.info("Sync closure status completed: %s", result)
    return result


async def run_closure_check(db: Session) -> dict:
    """Check and attempt auto-closure for all work orders.

    Optimized flow:
    1. Fetch all AITable records from 客户巡检派单 table
    2. Find records where 巡检是否完成='是' and 工单是否闭环!='是'
    3. For each record:
       - Check PTS work order status
       - If already closed in PTS, sync status to AITable
       - If not closed, attempt auto-closure (assign + note + advance stage)
       - Handle permission errors as "需人工处理"
    4. Update local database if work order exists

    Returns stats: checked, synced (from PTS), closed, manual, failed, skipped
    """
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"status": "skipped", "reason": "AITable not configured"}

    # 1. Fetch all AITable records
    records = await dingtalk_client.query_records(
        limit=200,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
    )

    logger.info("Closure check: %d AITable records fetched", len(records))

    # 2. Filter records that need processing
    to_process = []
    for record in records:
        cells = record.get("fields", {})
        inspection_complete = extract_select_name(cells.get(DISPATCH["巡检是否完成"]))
        closure_status = extract_select_name(cells.get(DISPATCH["工单是否闭环"]))

        if inspection_complete == "是" and closure_status != "是":
            link_val = cells.get(DISPATCH["巡检工单链接"])
            pts_order_id = _extract_pts_order_id(link_val)
            if pts_order_id:
                to_process.append({
                    "record_id": record.get("recordId") or record.get("record_id"),
                    "pts_order_id": pts_order_id,
                    "fields": cells,
                })

    logger.info("Closure check: %d records need processing", len(to_process))

    # 3. Process each record
    closed_count = 0
    manual_count = 0
    failed_count = 0
    synced_count = 0  # 从PTS同步的已闭环工单
    skipped_count = 0

    for item in to_process:
        pts_order_id = item["pts_order_id"]
        record_id = item["record_id"]
        fields = item["fields"]
        customer = fields.get(DISPATCH["客户名称"], "")
        report_attachments = fields.get(DISPATCH["巡检报告"])

        logger.info("Processing work order %s (%s)", pts_order_id, customer)

        try:
            # Check PTS status
            status = await pts_client.query_work_order_status(pts_order_id)
            if not status:
                logger.warning("Failed to query PTS status for %s", pts_order_id)
                failed_count += 1
                continue

            current_stage = status.get("current_stage", {})
            stage_name = current_stage.get("name", "")
            is_finished = status.get("is_finished", False)

            # Check if already closed in PTS
            if stage_name in _CLOSURE_STAGE_NAMES or is_finished:
                # Sync status to AITable
                try:
                    await dingtalk_client.update_records(
                        records=[{
                            "recordId": record_id,
                            "cells": {DISPATCH["工单是否闭环"]: "是"},
                        }],
                        base_id=settings.dt_dispatch_base_id,
                        table_id=settings.dt_dispatch_table_id,
                    )
                    logger.info("Synced PTS status to AITable for %s", pts_order_id)
                    synced_count += 1
                except Exception as e:
                    logger.error("Failed to update AITable for %s: %s", pts_order_id, e)
                    failed_count += 1

                # Update local database if exists
                wo = db.query(WorkOrder).filter(WorkOrder.pts_order_id == pts_order_id).first()
                if wo:
                    wo.closure_status = "已闭环"
                    db.commit()
                continue

            # Not closed in PTS, attempt auto-closure
            logger.info("Attempting auto-closure for %s", pts_order_id)

            # Check if work order exists in local DB
            wo = db.query(WorkOrder).filter(WorkOrder.pts_order_id == pts_order_id).first()
            if wo:
                wo.closure_status = "闭环中"
                db.commit()
                result = await _close_single_work_order(db, wo, report_attachments or [])
            else:
                # Work order not in local DB, create temporary one for closure
                temp_wo = WorkOrder(
                    pts_order_id=pts_order_id,
                    customer_name=customer,
                    closure_status="闭环中",
                )
                result = await _close_single_work_order(db, temp_wo, report_attachments or [])

            # Update based on result
            if result == "success":
                closed_count += 1
                logger.info("Successfully closed work order %s", pts_order_id)

                # Update AITable
                try:
                    await dingtalk_client.update_records(
                        records=[{
                            "recordId": record_id,
                            "cells": {DISPATCH["工单是否闭环"]: "是"},
                        }],
                        base_id=settings.dt_dispatch_base_id,
                        table_id=settings.dt_dispatch_table_id,
                    )
                except Exception as e:
                    logger.error("Failed to update AITable for %s: %s", pts_order_id, e)

                # Update local database
                if wo:
                    wo.closure_status = "已闭环"
                    db.commit()

            elif result == "manual":
                manual_count += 1
                logger.warning("Work order %s needs manual processing", pts_order_id)

                # Update AITable
                try:
                    await dingtalk_client.update_records(
                        records=[{
                            "recordId": record_id,
                            "cells": {DISPATCH["工单是否闭环"]: "需人工处理"},
                        }],
                        base_id=settings.dt_dispatch_base_id,
                        table_id=settings.dt_dispatch_table_id,
                    )
                except Exception as e:
                    logger.error("Failed to update AITable for %s: %s", pts_order_id, e)

                # Update local database
                if wo:
                    wo.closure_status = "需人工处理"
                    db.commit()

            else:  # failed
                failed_count += 1
                logger.warning("Failed to close work order %s", pts_order_id)

                # Update local database
                if wo:
                    wo.closure_status = "闭环失败"
                    db.commit()

        except Exception as e:
            logger.error("Error processing work order %s: %s", pts_order_id, e)
            failed_count += 1

    result = {
        "status": "success",
        "checked": len(to_process),
        "synced": synced_count,
        "closed": closed_count,
        "manual": manual_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }
    logger.info("Closure check completed: %s", result)
    return result


async def _close_single_work_order(
    db: Session,
    wo: WorkOrder,
    report_attachments: list[dict],
) -> str:
    """Close a single work order in PTS.

    Steps:
    1. Assign work order to 舒磊 (default assignee)
    2. Add note to PTS work order (referencing inspection report)
    3. Advance stage via confirm_work_order_stage until reaching 审核工单
    4. Log the trigger action

    Returns:
        "success" - 成功闭环
        "failed" - 失败
        "manual" - 需要人工处理（权限错误等）
    """
    # 1. Assign work order to 舒磊
    try:
        mutation = """
        mutation {
          update_work_order_claim_by(
            id: "%s",
            claim_by: "%s"
          )
        }
        """ % (wo.pts_order_id, _DEFAULT_ASSIGNEE_ID)
        result = await pts_client.pts_graphql_query(mutation)
        assign_result = result.get("update_work_order_claim_by", False)
        logger.info("Assigned work order %s to 舒磊: success=%s", wo.pts_order_id, assign_result)
    except Exception as e:
        logger.warning("Failed to assign work order %s: %s", wo.pts_order_id, e)
        # Continue even if assignment fails

    # 2. Add note to PTS work order
    attachment_names = []
    for att in report_attachments:
        if isinstance(att, dict):
            name = att.get("filename", "")
            if name:
                attachment_names.append(name)

    note_text = "巡检报告已上传至钉钉文档"
    if attachment_names:
        note_text += f"，附件: {', '.join(attachment_names)}"

    try:
        result = await pts_client.add_work_order_info(
            work_order_id=wo.pts_order_id,
            note=note_text,
        )
        logger.info("Added note to PTS work order %s: success=%s", wo.pts_order_id, result)
    except Exception as e:
        logger.error("Failed to add note to PTS work order %s: %s", wo.pts_order_id, e)

    # 2. Advance stage until reaching "审核工单"
    success_count = 0
    target_reached = False
    needs_manual = False  # 是否需要人工处理
    manual_reason = ""  # 人工处理原因

    for attempt in range(_MAX_STAGE_CONFIRM_ATTEMPTS):
        try:
            confirm_result = await pts_client.confirm_work_order_stage(wo.pts_order_id)
            logger.info(
                "Stage confirm attempt %d for %s: result=%s",
                attempt + 1, wo.pts_order_id, confirm_result,
            )

            if confirm_result is True:
                success_count += 1
                # Check if we've reached the target stage
                pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
                if pts_status:
                    current_stage = pts_status.get("current_stage") or {}
                    stage_name = current_stage.get("name", "")
                    if stage_name == _TARGET_CLOSURE_STAGE:
                        target_reached = True
                        logger.info(
                            "Reached target stage '%s' for work order %s",
                            stage_name, wo.pts_order_id,
                        )
                        break
                    elif stage_name in _CLOSURE_STAGE_NAMES:
                        # Already past target stage (shouldn't happen)
                        target_reached = True
                        logger.info(
                            "Work order %s already in closure stage '%s'",
                            wo.pts_order_id, stage_name,
                        )
                        break
                continue
            elif confirm_result is None or confirm_result is False:
                # Can't advance further, check current stage
                if success_count > 0:
                    pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
                    if pts_status:
                        current_stage = pts_status.get("current_stage") or {}
                        stage_name = current_stage.get("name", "")
                        if stage_name in _CLOSURE_STAGE_NAMES:
                            target_reached = True
                            logger.info(
                                "Work order %s in closure stage '%s' after %d attempts",
                                wo.pts_order_id, stage_name, success_count,
                            )
                if not target_reached:
                    logger.warning(
                        "Stage confirm returned %s for %s on attempt %d",
                        confirm_result, wo.pts_order_id, attempt + 1,
                    )
                break
        except Exception as e:
            error_msg = str(e)
            # 检测权限错误
            if "no permission" in error_msg or "需要设置负责人" in error_msg:
                needs_manual = True
                manual_reason = error_msg
                logger.warning(
                    "Work order %s needs manual processing: %s",
                    wo.pts_order_id, error_msg,
                )
            else:
                logger.error(
                    "Stage confirm error for %s on attempt %d: %s",
                    wo.pts_order_id, attempt + 1, e,
                )
            break

    # 3. Return result
    if needs_manual:
        _log_trigger(db, wo, "closure_manual", f"需要人工处理: {manual_reason}")
        return "manual"
    elif not target_reached:
        logger.error(
            "Failed to reach target stage '%s' for work order %s after %d attempts",
            _TARGET_CLOSURE_STAGE, wo.pts_order_id, success_count,
        )
        return "failed"

    _log_trigger(db, wo, "closure_success", f"巡检报告备注已添加，工单阶段推进到审核工单（推进{success_count}次）")
    return "success"


def _extract_pts_order_id(link_val) -> str | None:
    """Extract PTS order ID from AITable URL field.

    URL format: https://pts.chaitin.net/project/order/{pts_order_id}
    """
    url = None
    if isinstance(link_val, dict):
        url = link_val.get("link") or link_val.get("text", "")
    elif isinstance(link_val, str) and link_val.startswith("http"):
        url = link_val

    if not url:
        return None

    match = re.search(r'/project/order/([^/?]+)', url)
    return match.group(1) if match else None


def _log_trigger(db: Session, wo: WorkOrder, trigger_type: str, reason: str) -> None:
    """Log a trigger action for the work order closure."""
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=wo.id,
        trigger_type=trigger_type,
        trigger_reason=reason,
        status="success" if "success" in trigger_type else "failed",
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)


async def close_work_order_after_email(
    db: Session,
    record_id: str,
) -> dict:
    """Close a single work order after email is sent successfully.

    Looks up the work order by AITable dt_record_id, then:
    1. Fetches AITable record to get report attachments
    2. Closes the PTS work order (add note + advance stage)
    3. Updates local DB closure_status = "已闭环"
    4. Writes back 工单是否闭环='是' to AITable

    Returns {"success": bool, "message": str}
    """
    # 1. Find work order by dt_record_id
    wo = db.query(WorkOrder).filter(WorkOrder.dt_record_id == record_id).first()
    if not wo:
        logger.warning("No work order found for AITable record %s", record_id)
        return {"success": False, "message": f"未找到 AITable 记录 {record_id} 对应的工单"}

    if wo.closure_status == "已闭环":
        return {"success": True, "message": f"工单 {wo.pts_order_id} 已是已闭环状态，跳过"}

    # 2. Fetch AITable record to get report attachments
    settings = get_settings()
    try:
        records = await dingtalk_client.query_records(
            limit=100,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
            fetch_all=True,
        )
    except Exception as e:
        return {"success": False, "message": f"查询 AITable 失败: {e}"}

    target_record = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target_record = record
            break

    if not target_record:
        return {"success": False, "message": f"AITable 中未找到记录 {record_id}"}

    cells = target_record.get("fields", {})
    report_attachments = cells.get(DISPATCH["巡检报告"])

    # 3. Attempt closure
    wo.closure_status = "闭环中"
    db.commit()

    try:
        if isinstance(report_attachments, list) and len(report_attachments) > 0:
            result = await _close_single_work_order(db, wo, report_attachments)
        else:
            # No attachments, still try to advance stage
            result = await _close_single_work_order(db, wo, [])

        if result == "success":
            wo.closure_status = "已闭环"
            db.commit()
            logger.info("Successfully closed work order %s after email send", wo.pts_order_id)
        elif result == "manual":
            wo.closure_status = "需人工处理"
            db.commit()
            return {"success": False, "message": f"工单 {wo.pts_order_id} 需要人工处理"}
        else:  # failed
            wo.closure_status = "闭环失败"
            db.commit()
            return {"success": False, "message": f"工单 {wo.pts_order_id} 闭环失败"}
    except Exception as e:
        wo.closure_status = "闭环失败"
        db.commit()
        logger.error("Closure error for work order %s: %s", wo.pts_order_id, e)
        return {"success": False, "message": f"工单闭环异常: {e}"}

    # 4. Write back 工单是否闭环='是' to AITable
    try:
        await dingtalk_client.update_records(
            records=[{
                "recordId": record_id,
                "cells": {
                    DISPATCH["工单是否闭环"]: "是",
                },
            }],
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
        )
        logger.info("Wrote back 工单是否闭环='是' to AITable: record=%s", record_id)
    except Exception as e:
        logger.error("Failed to write back 工单是否闭环 to AITable: %s", e)

    # 5. Broadcast update
    try:
        from apps.api.routers.ws import broadcaster
        await broadcaster.broadcast("work_order.closure_updated", {
            "pts_order_id": wo.pts_order_id,
            "closure_status": "已闭环",
            "customer_name": wo.customer_name,
        })
    except Exception:
        pass

    return {"success": True, "message": f"工单 {wo.pts_order_id} 已闭环"}
