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


async def _run_closure_check_v1(db: Session) -> dict:
    """Check and attempt auto-closure for all work orders.

    Event-driven flow (triggered by AITable field changes):
    1. Fetch all AITable records from 客户巡检派单 table
    2. Find records where 邮件是否发送='是' and 巡检报告不为空 and 工单是否闭环!='是'
    3. For each record:
       - Check PTS work order status
       - If already closed in PTS, sync status to AITable
       - If not closed, attempt auto-closure (assign + note + advance stage)
       - Handle permission errors as "需人工处理"
    4. Update local database if work order exists
    5. Recovery: detect local DB "已闭环" records where PTS is actually still open,
       and reset them to "未闭环"

    Returns stats: checked, synced (from PTS), closed, manual, failed, skipped, recovered
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
    #    条件：邮件是否发送='是'（系统自动写回，可靠）+ 巡检报告不为空 + 工单是否闭环≠'是'
    to_process = []
    for record in records:
        cells = record.get("cells", {})
        email_sent = extract_select_name(cells.get(DISPATCH["邮件是否发送"]))
        closure_status = extract_select_name(cells.get(DISPATCH["工单是否闭环"]))
        report_attachments = cells.get(DISPATCH["巡检报告"])
        has_report = isinstance(report_attachments, list) and len(report_attachments) > 0

        if email_sent == "是" and closure_status != "是" and has_report:
            link_val = cells.get(DISPATCH["巡检工单链接"])
            pts_order_id = _extract_pts_order_id(link_val)
            if pts_order_id:
                to_process.append({
                    "record_id": record.get("recordId") or record.get("record_id"),
                    "pts_order_id": pts_order_id,
                    "cells": cells,
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
        cells = item["cells"]
        customer = cells.get(DISPATCH["客户名称"], "")
        report_attachments = cells.get(DISPATCH["巡检报告"])

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

    # ── Recovery: detect and fix inconsistent local DB states ──
    # Find work orders where local says "已闭环" but PTS says otherwise.
    # This catches data corrupted by premature closure_status updates.
    recovered_count = 0
    processed_ids = {item["pts_order_id"] for item in to_process}

    stale_orders = db.query(WorkOrder).filter(
        WorkOrder.closure_status == "已闭环",
    ).all()

    for wo in stale_orders:
        if not wo.pts_order_id or wo.pts_order_id.startswith("dt_"):
            continue
        if wo.pts_order_id in processed_ids:
            # Already handled in the AITable loop above
            continue

        try:
            pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
            if not pts_status:
                continue

            stage_name = (pts_status.get("current_stage") or {}).get("name", "")
            is_finished = pts_status.get("is_finished", False)

            if not (is_finished or stage_name in _CLOSURE_STAGE_NAMES):
                # Local says "已闭环" but PTS says still open → reset
                logger.warning(
                    "Recovery: work order %s local=已闭环 but PTS stage=%s, resetting",
                    wo.pts_order_id, stage_name,
                )
                wo.closure_status = "未闭环"
                db.commit()
                recovered_count += 1
        except Exception as e:
            logger.error("Recovery: failed to check PTS for %s: %s", wo.pts_order_id, e)

    result = {
        "status": "success",
        "checked": len(to_process),
        "synced": synced_count,
        "closed": closed_count,
        "manual": manual_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "recovered": recovered_count,
    }
    logger.info("Closure check completed: %s", result)
    return result


async def run_closure_check(db: Session) -> dict:
    """Dispatch to the isolated V2 coordinator only when explicitly enabled."""
    if get_settings().inspection_closure_v2_enabled:
        from services.inspection_closure_v2 import run_closure_check_v2

        return await run_closure_check_v2(db)
    return await _run_closure_check_v1(db)


async def _close_single_work_order(
    db: Session,
    wo: WorkOrder,
    report_attachments: list[dict],
) -> str:
    """Close a single work order in PTS.

    Three scenarios based on claim_by (工单负责人) and current stage:

    场景1: claim_by是舒磊 → 添加舒磊到项目成员 + 推进到审核工单
    场景2: claim_by是其他人，但阶段已到"开始处理工单"或之后
           → 修改负责人为舒磊 + 添加舒磊到项目成员 + 推进到审核工单
    场景3: claim_by是其他人，阶段还在"指定工单负责人"
           → 添加舒磊到项目成员 + 钉钉通知用户(冯伟)去找创建人推进阶段
           → 返回"manual"，等创建人指定负责人后再下次闭环检查处理

    Returns:
        "success" - 成功闭环
        "failed" - 失败
        "manual" - 需要人工处理（场景3：钉钉通知已发）
    """
    # ── Step 0: Query PTS for claim_by, current stage, and existing info ──
    claim_by_id = ""
    claim_by_name = ""
    creator_id = ""
    creator_name = ""
    stage_name = ""
    stage_sequence = 0
    existing_info: list[dict] = []
    try:
        query = """
        {
          workOrderByID(id: \"%s\") {
            id
            is_finished
            current_stage { name sequence }
            creator { id name username }
            claim_by { id name username }
            info { id note file { id filename } }
          }
        }
        """ % wo.pts_order_id
        result = await pts_client.pts_graphql_query(query)
        pts_status = result.get("workOrderByID") if result else None
        if pts_status:
            creator = pts_status.get("creator") or {}
            creator_id = creator.get("id", "")
            creator_name = creator.get("name", "")
            claim_by = pts_status.get("claim_by") or {}
            claim_by_id = claim_by.get("id", "")
            claim_by_name = claim_by.get("name", "")
            current_stage = pts_status.get("current_stage") or {}
            stage_name = current_stage.get("name", "")
            stage_sequence = current_stage.get("sequence", 0)
            existing_info = pts_status.get("info") or []
            logger.info(
                "Work order %s: creator=%s(%s), claim_by=%s(%s), stage=%s(seq=%s), info_count=%d",
                wo.pts_order_id, creator_name, creator_id,
                claim_by_name, claim_by_id,
                stage_name, stage_sequence, len(existing_info),
            )
    except Exception as e:
        logger.warning("Failed to query PTS status for %s: %s", wo.pts_order_id, e)
        # Fall through — will attempt best-effort below

    # ── Dedup check: skip report upload if already uploaded ──
    already_has_report = False
    for info_entry in existing_info:
        note = info_entry.get("note", "")
        files = info_entry.get("file") or []
        if "/f/" in note and ("巡检报告" in note or any(
            f.get("filename", "").lower().endswith((".pdf", ".doc", ".docx"))
            for f in files if isinstance(f, dict)
        )):
            already_has_report = True
            break
        if files and any(
            f.get("filename", "").lower().endswith((".pdf", ".doc", ".docx"))
            for f in files if isinstance(f, dict)
        ):
            already_has_report = True
            break

    if already_has_report and not pts_file_ids_needs_reupload(report_attachments, existing_info):
        logger.info(
            "Work order %s already has inspection report uploaded, skipping duplicate upload",
            wo.pts_order_id,
        )

    # ── Step 1: Determine scenario and handle assignment ──
    is_claim_by_shulei = (claim_by_id == _DEFAULT_ASSIGNEE_ID)
    # "开始处理工单" is the stage after "指定工单负责人" (sequence >= 2)
    _START_PROCESSING_STAGE = "开始处理工单"
    is_at_or_after_start_processing = (
        stage_sequence >= 2 or stage_name == _START_PROCESSING_STAGE
        or stage_name in _CLOSURE_STAGE_NAMES
    )

    # ── Always add 舒磊 to project members first ──
    logger.info("Adding 舒磊 to project members for work order %s", wo.pts_order_id)
    try:
        member_added = await pts_client.add_work_order_member(
            wo.pts_order_id, _DEFAULT_ASSIGNEE_ID,
        )
        if member_added:
            logger.info("Added 舒磊 as project member for work order %s", wo.pts_order_id)
        else:
            logger.info("add_work_order_member returned False for %s (may already be a member), continuing", wo.pts_order_id)
    except Exception as e:
        logger.warning("Failed to add 舒磊 as member for work order %s: %s", wo.pts_order_id, e)

    if is_claim_by_shulei:
        # ── 场景1: claim_by是舒磊 → 直接推进到审核工单 ──
        logger.info("场景1: claim_by是舒磊, 推进工单 %s 到审核工单", wo.pts_order_id)

    elif not is_claim_by_shulei and is_at_or_after_start_processing:
        # ── 场景2: claim_by是其他人，阶段已到"开始处理工单"或之后
        #    → 修改负责人为舒磊 + 推进到审核工单 ──
        logger.info(
            "场景2: claim_by=%s(%s), 阶段=%s(seq=%s), 修改负责人为舒磊并推进",
            claim_by_name, claim_by_id, stage_name, stage_sequence,
        )
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
            assign_success = result.get("update_work_order_claim_by", False)
            logger.info("Changed claim_by to 舒磊 for %s: success=%s", wo.pts_order_id, assign_success)
        except Exception as e:
            error_msg = str(e)
            logger.error("Failed to change claim_by for %s: %s", wo.pts_order_id, error_msg)
            if "no permission" in error_msg or "需要设置负责人" in error_msg:
                _log_trigger(db, wo, "closure_manual", f"无法修改负责人为舒磊: {error_msg}")
                return "manual"
            return "failed"

    else:
        # ── 场景3: claim_by是其他人，阶段还在"指定工单负责人"
        #    → 已添加项目成员 + 钉钉通知用户去找创建人推进阶段 ──
        logger.info(
            "场景3: claim_by=%s(%s), 阶段=%s, 需创建人(%s)指定负责人, 发钉钉通知",
            claim_by_name, claim_by_id, stage_name, creator_name,
        )
        pts_url = f"https://pts.chaitin.net/project/order/{wo.pts_order_id}"
        reason = (
            f"工单负责人为{claim_by_name}，当前阶段还在「{stage_name}」，"
            f"需要创建人{creator_name}指定负责人为舒磊后才能继续闭环"
        )
        _log_trigger(db, wo, "closure_manual", reason)

        # 发钉钉消息通知用户
        try:
            from services.dingtalk_notifier import send_dingtalk_notification
            await send_dingtalk_notification(
                title="⚠️ 巡检工单需要人工推进阶段",
                content=(
                    f"工单 [{wo.pts_order_id}]({pts_url})（客户：{wo.customer_name}）\n\n"
                    f"- 工单负责人：{claim_by_name}\n"
                    f"- 当前阶段：「{stage_name}」\n"
                    f"- 创建人：{creator_name}\n\n"
                    f"需要创建人 **{creator_name}** 指定负责人为舒磊，才能继续自动闭环。\n\n"
                    f"已将舒磊添加到项目成员，请找 **{creator_name}** 推进工单阶段。"
                ),
            )
            logger.info("DingTalk notification sent for work order %s (scenario 3)", wo.pts_order_id)
        except Exception as e:
            logger.warning("Failed to send DingTalk notification for work order %s: %s", wo.pts_order_id, e)

        return "manual"

    # ── Step 2: Upload inspection reports (skip if already uploaded) ──
    pts_file_ids: list[str] = []
    if not already_has_report and isinstance(report_attachments, list) and len(report_attachments) > 0:
        try:
            pts_file_ids = await pts_client.download_and_upload_reports(report_attachments)
            if pts_file_ids:
                logger.info(
                    "Uploaded %d report(s) to PTS for work order %s: %s",
                    len(pts_file_ids), wo.pts_order_id, pts_file_ids,
                )
            else:
                logger.warning("No reports successfully uploaded for work order %s", wo.pts_order_id)
        except Exception as e:
            logger.error("Report upload failed for work order %s: %s", wo.pts_order_id, e)

    # ── Step 3: Add note to PTS work order ──
    # PTS web UI renders [filename](/f/{file_id}) as clickable download links.
    if pts_file_ids:
        note_text = f"巡检报告已上传（{len(pts_file_ids)}个附件）"
        for att, fid in zip(report_attachments, pts_file_ids):
            if isinstance(att, dict):
                filename = att.get("filename", "巡检报告")
                note_text += f"\n[{filename}](/f/{fid})"
    elif already_has_report:
        note_text = "自动闭环：巡检报告已在前次上传"
    else:
        note_text = "巡检报告已上传至钉钉文档"
        attachment_names = []
        for att in report_attachments:
            if isinstance(att, dict):
                name = att.get("filename", "")
                if name:
                    attachment_names.append(name)
        if attachment_names:
            note_text += f"，附件: {', '.join(attachment_names)}"

    try:
        note_result = await pts_client.add_work_order_info(
            work_order_id=wo.pts_order_id,
            note=note_text,
            file_ids=None,  # Markdown links in note text are the correct way for PTS web UI
        )
        logger.info("Added note to PTS work order %s: success=%s (file_ids=%s)", wo.pts_order_id, note_result, pts_file_ids)
    except Exception as e:
        logger.error("Failed to add note to PTS work order %s: %s", wo.pts_order_id, e)

    # ── Step 4: Advance stage until reaching "审核工单" ──
    # Key insight: at the "指定工单负责人" stage, confirm_work_order_stage
    # requires the claim_by parameter to set the responsible person.
    # Without it, PTS returns "需要设置负责人" error.
    # For subsequent stages, claim_by is not needed.
    success_count = 0
    target_reached = False
    needs_manual = False
    manual_reason = ""
    # Pass claim_by on the first call if we're starting from "指定工单负责人"
    claim_by_for_first_confirm = _DEFAULT_ASSIGNEE_ID if stage_name == "指定工单负责人" else None

    for attempt in range(_MAX_STAGE_CONFIRM_ATTEMPTS):
        try:
            # Only pass claim_by on the first attempt if starting at "指定工单负责人"
            claim_by_arg = claim_by_for_first_confirm if attempt == 0 else None
            confirm_result = await pts_client.confirm_work_order_stage(
                wo.pts_order_id, claim_by=claim_by_arg,
            )
            logger.info(
                "Stage confirm attempt %d for %s: result=%s (claim_by=%s)",
                attempt + 1, wo.pts_order_id, confirm_result, claim_by_arg,
            )

            if confirm_result is True:
                success_count += 1
                pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
                if pts_status:
                    current_stage = pts_status.get("current_stage") or {}
                    cur_stage_name = current_stage.get("name", "")
                    if cur_stage_name == _TARGET_CLOSURE_STAGE:
                        target_reached = True
                        logger.info(
                            "Reached target stage '%s' for work order %s",
                            cur_stage_name, wo.pts_order_id,
                        )
                        break
                    elif cur_stage_name in _CLOSURE_STAGE_NAMES:
                        target_reached = True
                        logger.info(
                            "Work order %s already in closure stage '%s'",
                            wo.pts_order_id, cur_stage_name,
                        )
                        break
                continue
            elif confirm_result is None or confirm_result is False:
                pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
                if pts_status:
                    current_stage = pts_status.get("current_stage") or {}
                    cur_stage_name = current_stage.get("name", "")
                    if cur_stage_name in _CLOSURE_STAGE_NAMES:
                        target_reached = True
                        logger.info(
                            "Work order %s in closure stage '%s' (PTS returned %s but stage advanced, attempt %d)",
                            wo.pts_order_id, cur_stage_name, confirm_result, attempt + 1,
                        )
                if not target_reached:
                    logger.warning(
                        "Stage confirm returned %s for %s on attempt %d, stage did not advance",
                        confirm_result, wo.pts_order_id, attempt + 1,
                    )
                break
        except Exception as e:
            error_msg = str(e)
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

    # ── Step 5: Return result ──
    if needs_manual:
        _log_trigger(db, wo, "closure_manual", f"需要人工处理: {manual_reason}")
        return "manual"
    elif not target_reached:
        logger.error(
            "Failed to reach target stage '%s' for work order %s after %d attempts",
            _TARGET_CLOSURE_STAGE, wo.pts_order_id, success_count,
        )
        return "failed"

    _log_trigger(db, wo, "closure_success", f"巡检报告已上传（{len(pts_file_ids)}个文件），工单阶段推进到审核工单（推进{success_count}次）")
    return "success"


def _extract_existing_report_filenames(existing_info: list[dict]) -> set[str]:
    """Extract filenames of already-uploaded inspection reports from PTS info entries."""
    filenames: set[str] = set()
    for info_entry in existing_info:
        files = info_entry.get("file") or []
        for f in files:
            if isinstance(f, dict):
                fn = f.get("filename", "")
                if fn:
                    filenames.add(fn)
        # Also extract filenames from Markdown links in notes [/f/...]
        note = info_entry.get("note", "")
        import re
        for match in re.finditer(r'\[([^\]]+)\]\(/f/[^\)]+\)', note):
            filenames.add(match.group(1))
    return filenames


def pts_file_ids_needs_reupload(
    report_attachments: list[dict],
    existing_info: list[dict],
) -> bool:
    """Check whether report attachments need to be re-uploaded.

    Returns True if any attachment filename is not already present in the
    PTS work order's info entries (meaning it hasn't been uploaded yet).
    Returns False if all filenames already exist (no re-upload needed).
    """
    existing_filenames = _extract_existing_report_filenames(existing_info)
    for att in report_attachments:
        if isinstance(att, dict):
            fn = att.get("filename", "")
            if fn and fn not in existing_filenames:
                return True
    return False


def recover_stalled_closure_wip(db: Session) -> int:
    """Reset work orders stuck in '闭环中' back to '未闭环'.

    A work order in '闭环中' for more than 10 minutes is considered stalled
    (the process likely crashed or was restarted mid-closure). Reset it so
    the next closure check can retry.

    Called at app startup, alongside cleanup_stale_running_logs.

    Returns the number of recovered work orders.
    """
    from datetime import timedelta

    from models.work_order import WorkOrder

    stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
    stalled = db.query(WorkOrder).filter(
        WorkOrder.closure_status == "闭环中",
        WorkOrder.updated_at < stale_threshold,
    ).all()

    if not stalled:
        return 0

    for wo in stalled:
        logger.warning(
            "Recovering stalled work order %s (%s): '闭环中' for >10min, resetting to '未闭环'",
            wo.pts_order_id, wo.customer_name,
        )
        wo.closure_status = "未闭环"

    db.commit()
    logger.warning("Recovered %d stalled '闭环中' work orders", len(stalled))
    return len(stalled)


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
    """Log a trigger action for the work order closure.

    If the WorkOrder is a temporary object not persisted in the database
    (e.g., created ad-hoc in run_closure_check for an AITable-only record),
    write the trigger log with work_order_id=None to avoid FK violations.
    """
    from sqlalchemy.orm import object_session

    in_session = object_session(wo) is not None
    log = TriggerLog(
        id=uuid.uuid4(),
        work_order_id=wo.id if in_session else None,
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
        # Verify against PTS — local status may be stale
        try:
            pts_status = await pts_client.query_work_order_status(wo.pts_order_id)
            if pts_status:
                stage_name = (pts_status.get("current_stage") or {}).get("name", "")
                if pts_status.get("is_finished") or stage_name in _CLOSURE_STAGE_NAMES:
                    return {"success": True, "message": f"工单 {wo.pts_order_id} 已在PTS闭环，跳过"}
                else:
                    logger.warning(
                        "Work order %s: local says 已闭环 but PTS stage=%s, resetting and proceeding",
                        wo.pts_order_id, stage_name,
                    )
                    wo.closure_status = "未闭环"
                    db.commit()
        except Exception as e:
            logger.warning("Failed to verify PTS status for %s, proceeding with closure: %s", wo.pts_order_id, e)

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

    cells = target_record.get("cells", {})
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


async def handle_email_success(
    db: Session,
    record_id: str,
    *,
    legacy_closure: bool = True,
) -> dict:
    """Run the configured post-email closure policy.

    Existing email-tool paths retain their V1 behavior when V2 is disabled;
    callers that historically did not close (such as monitor manual email)
    can pass ``legacy_closure=False`` and gain only the opt-in V2 behavior.
    """
    settings = get_settings()
    if settings.inspection_closure_v2_enabled:
        from services.inspection_closure_v2 import coordinate_after_email_success

        return await coordinate_after_email_success(db, record_id)
    if legacy_closure:
        return await close_work_order_after_email(db, record_id)
    return {"success": True, "status": "skipped", "message": "旧版入口未配置闭环"}
