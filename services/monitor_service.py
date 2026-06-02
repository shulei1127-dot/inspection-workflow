"""Monitor service: poll DingTalk AITable for changes and trigger actions.

Three monitoring loops:

1. run_monitor_poll — 客户巡检派单 table
   - Sync AITable fields back to local work_orders
   - Delete completion-stage work orders (from both AITable and local DB)
   - Trigger yunji dispatch when engineer + supplier filled + dispatch_status='待派单'
   - Trigger email when email_sent='否' and email_trigger_status='待发送'

2. run_dispatch_monitor_poll — 客户巡检派单 table
   - Check dispatch condition: 伙伴供应商 filled + 需求编号 empty + 工程师 not empty
   - Find PTS link from matching WorkOrder by customer_name
   - Call yunji dispatch with ptsUrl and supplier
   - Write back demandId → 需求编号 and orderId → 订单编号

3. Email pending — 客户巡检派单 table
   - Check email condition: 巡检是否完成='是' + 巡检报告 has attachment + 邮件是否发送!='是'
   - Download attachment and send email
   - Write back 邮件是否发送 → '是'

All AITable field IDs are defined in services.aitable_fields module.
"""

import logging
import time

from sqlalchemy.orm import Session

from models.work_order import WorkOrder
from services import dingtalk_client
from services.aitable_fields import (
    DISPATCH,
    extract_text, extract_select_name, extract_engineer,
    COMPLETION_STAGES, current_month,
)

logger = logging.getLogger(__name__)

# ── AITable query cache (avoid repeated slow dws CLI calls) ──
_aitable_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 30  # seconds

# ── Email pending probe cache (refreshed by scheduled job every 2h) ──
_email_pending_cache: tuple[float, dict] | None = None  # (timestamp, result)
_EMAIL_CACHE_TTL = 2 * 3600  # 2 hours


def _invalidate_email_cache() -> None:
    """Clear the email pending cache so next request fetches fresh data."""
    global _email_pending_cache
    _email_pending_cache = None


def invalidate_all_caches() -> None:
    """Clear all caches (AITable query cache + email pending cache).
    Called when user explicitly requests a refresh from the UI.
    """
    global _email_pending_cache
    _email_pending_cache = None
    _aitable_cache.clear()


def _invalidate_aitable_cache(base_id: str, table_id: str) -> None:
    """Clear the AITable query cache for a specific table."""
    cache_key = f"{base_id}/{table_id}"
    _aitable_cache.pop(cache_key, None)


async def _cached_query_records(
    base_id: str,
    table_id: str,
    fetch_all: bool = True,
) -> list[dict]:
    """Query AITable with a short-lived cache to avoid redundant dws calls."""
    cache_key = f"{base_id}/{table_id}"
    now = time.time()
    if cache_key in _aitable_cache:
        ts, records = _aitable_cache[cache_key]
        if now - ts < _CACHE_TTL:
            logger.debug("Cache hit for %s (%d records, age %.1fs)", cache_key, len(records), now - ts)
            return records
    records = await dingtalk_client.query_records(
        limit=100,
        base_id=base_id,
        table_id=table_id,
        fetch_all=fetch_all,
    )
    _aitable_cache[cache_key] = (now, records)
    logger.debug("Cache miss for %s, fetched %d records", cache_key, len(records))
    return records

async def run_monitor_poll(db: Session) -> dict:
    """Run a single monitoring poll cycle (reads from 客户巡检派单 table)."""
    from core.config import get_settings
    settings = get_settings()
    logger.info("Monitor poll: base_id=%s table_id=%s", settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"status": "skipped", "reason": "AITable not configured"}

    # 1. Query AITable records (paginate to fetch all) from 客户巡检派单 table
    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )
    logger.info("Monitor poll: fetched %d records from AITable", len(records))
    if not records:
        return {"status": "no_records", "dispatch_triggered": 0, "email_triggered": 0}

    # 2. Match and detect changes
    dispatch_triggered = 0
    email_triggered = 0

    for record in records:
        record_id = record.get("recordId") or record.get("record_id", "")
        if not record_id:
            continue

        # Find matching local work order
        wo = db.query(WorkOrder).filter(WorkOrder.dt_record_id == record_id).first()
        if not wo:
            continue

        cells = record.get("cells", {})

        # Sync AITable fields back to local
        _sync_from_aitable(wo, cells)

        # Skip and delete work orders in completion stage (already processed)
        if wo.status in COMPLETION_STAGES:
            # Delete AITable record + local DB record
            if wo.dt_record_id:
                try:
                    await dingtalk_client.delete_records(
                        record_ids=wo.dt_record_id,
                        base_id=settings.dt_dispatch_base_id,
                        table_id=settings.dt_dispatch_table_id,
                    )
                    logger.info("Deleted AITable record %s for completed work order %s", wo.dt_record_id, wo.pts_order_id)
                except Exception as e:
                    logger.error("Failed to delete AITable record %s: %s", wo.dt_record_id, e)
            db.delete(wo)
            db.commit()
            continue

        # Check dispatch trigger condition (only if auto dispatch is enabled)
        if settings.auto_dispatch_enabled:
            has_engineer = bool(wo.engineer and wo.engineer.strip())
            if not has_engineer:
                eng_from_cells = extract_engineer(cells.get(DISPATCH["工程师"]))
                has_engineer = bool(eng_from_cells and eng_from_cells.strip())
            has_supplier = bool(wo.partner_supplier and wo.partner_supplier.strip())
            if not has_supplier:
                sup_from_cells = extract_select_name(cells.get(DISPATCH["伙伴供应商"]))
                has_supplier = bool(sup_from_cells and sup_from_cells.strip())
            if has_engineer and has_supplier and wo.dispatch_status == "待派单":
                from services.trigger_service import trigger_yunji_dispatch
                try:
                    result = await trigger_yunji_dispatch(db, wo.id)
                    if result.get("status") == "success":
                        wo.dispatch_status = "已派单"
                        dispatch_triggered += 1
                    else:
                        wo.dispatch_status = "派单失败"
                except Exception as e:
                    logger.error("Yunji dispatch failed for %s: %s", wo.pts_order_id, e)
                    wo.dispatch_status = "派单失败"

        # Check email trigger condition (only if auto email is enabled)
        if settings.auto_email_enabled and wo.email_sent == "否" and wo.email_trigger_status == "待发送":
            from services.trigger_service import trigger_email_send
            try:
                result = await trigger_email_send(db, wo.id)
                if result.get("status") == "success":
                    wo.email_trigger_status = "已发送"
                    email_triggered += 1
                else:
                    wo.email_trigger_status = "发送失败"
            except Exception as e:
                logger.error("Email send failed for %s: %s", wo.pts_order_id, e)
                wo.email_trigger_status = "发送失败"

    db.commit()

    result = {
        "status": "success",
        "records_checked": len(records),
        "dispatch_triggered": dispatch_triggered,
        "email_triggered": email_triggered,
    }

    await _broadcast_monitor("monitor.poll.completed", result)

    return result


def _sync_from_aitable(wo: WorkOrder, cells: dict) -> None:
    """Sync field values from AITable back to local work order."""
    # Engineer field (user type): list of {userId, corpId} or {userName, userRef}
    engineer_val = cells.get(DISPATCH["工程师"])
    if engineer_val:
        if isinstance(engineer_val, list) and len(engineer_val) > 0:
            names = []
            for u in engineer_val:
                if not isinstance(u, dict):
                    continue
                name = u.get("userId") or u.get("userName") or u.get("userRef", "")
                if name:
                    names.append(name)
            wo.engineer = ", ".join(names) if names else None
        elif isinstance(engineer_val, str):
            wo.engineer = engineer_val

    # Partner supplier field (singleSelect): {id, name}
    supplier_val = cells.get(DISPATCH["伙伴供应商"])
    if supplier_val:
        if isinstance(supplier_val, dict):
            wo.partner_supplier = supplier_val.get("name", str(supplier_val))
        elif isinstance(supplier_val, str):
            wo.partner_supplier = supplier_val

    # Email sent field (singleSelect): {id, name}
    email_val = cells.get(DISPATCH["邮件是否发送"])
    if email_val:
        if isinstance(email_val, dict):
            wo.email_sent = email_val.get("name", str(email_val))
        elif isinstance(email_val, str):
            wo.email_sent = email_val

    # Dispatch level (派单等级) — store in raw_data, NOT dispatch_status.
    # dispatch_status tracks lifecycle: 待派单 → 已派单 → 派单失败.
    # 派单等级 is a priority (L1/L2/L3) and must not overwrite it.
    level_val = cells.get(DISPATCH["派单等级"])
    if level_val:
        if isinstance(level_val, dict):
            level_name = level_val.get("name", str(level_val))
        elif isinstance(level_val, str):
            level_name = level_val
        else:
            level_name = str(level_val)
        if level_name:
            raw = wo.raw_data or {}
            raw["派单等级"] = level_name
            wo.raw_data = raw
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(wo, "raw_data")


# ── Dispatch monitor (客户巡检派单 table) ──────────────────────────────────────


async def run_dispatch_monitor_poll(db: Session) -> dict:
    """Monitor 客户巡检派单 AITable and trigger yunji dispatch.

    Dispatch condition (all must be true):
    1. 伙伴供应商 (hCJ8nkj) is filled
    2. 需求编号 (AntmbXo) is empty (not yet dispatched)
    3. 工程师 (p40jtpC) is not empty

    After successful dispatch:
    - Write back demandId → AntmbXo (需求编号)
    - Write back orderId → LyTZGs5 (订单编号)
    """
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"status": "skipped", "reason": "Dispatch AITable not configured"}

    # 1. Fetch all records (dws --filters not reliable, filter locally)
    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )
    logger.info("Dispatch monitor poll: fetched %d records", len(records))

    if not records:
        return {"status": "no_records", "dispatch_triggered": 0}

    dispatch_triggered = 0
    dispatch_failed = 0

    for record in records:
        record_id = record.get("recordId") or record.get("record_id", "")
        if not record_id:
            continue

        cells = record.get("cells", {})

        # Sync AITable fields back to local DB for dashboard consistency
        wo = db.query(WorkOrder).filter(WorkOrder.dt_record_id == record_id).first()
        if wo:
            _sync_from_aitable(wo, cells)

        # Extract key field values
        supplier = extract_select_name(cells.get(DISPATCH["伙伴供应商"]))
        engineer = extract_engineer(cells.get(DISPATCH["工程师"]))
        customer_name = extract_text(cells.get(DISPATCH["客户名称"]))
        demand_number = extract_text(cells.get(DISPATCH["需求编号"]))

        # Check dispatch condition: supplier filled + engineer filled + demand_number empty
        has_supplier = bool(supplier and supplier.strip())
        has_engineer = bool(engineer and engineer.strip())
        no_demand = not (demand_number and demand_number.strip())

        if not (has_supplier and has_engineer and no_demand):
            continue

        logger.info(
            "Dispatch condition met for record %s: supplier=%s, engineer=%s, customer=%s",
            record_id, supplier, engineer, customer_name,
        )

        # 2. Find PTS link from matching WorkOrder by customer_name
        pts_url = await _find_pts_url(db, customer_name, cells)
        if not pts_url:
            logger.warning(
                "Dispatch: no PTS URL found for record %s (customer=%s), writing error marker",
                record_id, customer_name,
            )
            await _write_back_pts_url_missing(
                record_id=record_id,
                customer_name=customer_name,
                base_id=settings.dt_dispatch_base_id,
                table_id=settings.dt_dispatch_table_id,
            )
            dispatch_failed += 1
            continue

        # Idempotency check: skip if already dispatched successfully for this record
        from models.trigger_log import TriggerLog
        existing_log = db.query(TriggerLog).filter(
            TriggerLog.trigger_type == "yunji_dispatch",
            TriggerLog.trigger_reason.contains(f"record={record_id}"),
            TriggerLog.status == "success",
        ).first()
        if existing_log:
            logger.info(
                "Dispatch: record %s already dispatched (log %s), skipping",
                record_id, existing_log.id,
            )
            continue

        # 3. Call yunji dispatch
        from services.trigger_service import dispatch_from_aitable
        try:
            result = await dispatch_from_aitable(
                db,
                pts_url=pts_url,
                supplier=supplier,
                record_id=record_id,
                customer_name=customer_name,
            )

            if result.get("status") == "success":
                demand_id = result.get("demandId", "")
                order_id = result.get("orderId", "")
                logger.info(
                    "Dispatch success for record %s: demandId=%s, orderId=%s",
                    record_id, demand_id, order_id,
                )

                # 4. Write back demandId and orderId to AITable
                await _write_back_dispatch_result(
                    record_id=record_id,
                    demand_id=demand_id,
                    order_id=order_id,
                    base_id=settings.dt_dispatch_base_id,
                    table_id=settings.dt_dispatch_table_id,
                )
                _invalidate_aitable_cache(settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
                dispatch_triggered += 1
            else:
                logger.warning(
                    "Dispatch failed for record %s: %s",
                    record_id, result.get("message", "unknown error"),
                )
                dispatch_failed += 1
        except Exception as e:
            logger.error("Dispatch trigger failed for record %s: %s", record_id, e)
            dispatch_failed += 1

    db.commit()

    result = {
        "status": "success",
        "records_checked": len(records),
        "dispatch_triggered": dispatch_triggered,
        "dispatch_failed": dispatch_failed,
    }

    await _broadcast_monitor("monitor.dispatch_poll.completed", result)

    return result


async def _lookup_dispatch_emails(customer_name: str | None) -> list[str]:
    """Look up 报告发送邮箱 from DISPATCH table by customer name."""
    if not customer_name:
        return []
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return []
    try:
        records = await _cached_query_records(
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
        )
        emails = []
        for record in records:
            cells = record.get("cells", {})
            rec_name = extract_text(cells.get(DISPATCH["客户名称"]))
            if rec_name != customer_name:
                continue
            dispatch_email = extract_text(cells.get(DISPATCH["报告发送邮箱"]))
            if dispatch_email:
                for addr in dispatch_email.replace("、", ",").replace("；", ",").split(","):
                    addr = addr.strip()
                    if addr and "@" in addr and addr not in emails:
                        emails.append(addr)
        return emails
    except Exception as e:
        logger.warning("Failed to look up dispatch emails for %s: %s", customer_name, e)
        return []


async def _find_pts_url(db: Session, customer_name: str | None, cells: dict) -> str | None:
    """Find PTS order URL for dispatch.

    Strategy:
    1. Try to match a local WorkOrder by customer_name → use pts_order_url
    2. If no match, try to extract from 客户巡检派单 AITable by customer_name
    """
    # Strategy 1: Match by customer_name in local DB
    if customer_name:
        wo = db.query(WorkOrder).filter(
            WorkOrder.customer_name == customer_name,
        ).first()
        if wo and wo.pts_order_url:
            return wo.pts_order_url
        if wo and wo.pts_order_id:
            return f"https://pts.chaitin.net/project/order/{wo.pts_order_id}"

    # Strategy 2: Try extracting from current cells (DISPATCH table already has 巡检工单链接)
    link_val = cells.get(DISPATCH["巡检工单链接"])
    if link_val:
        if isinstance(link_val, dict):
            url = link_val.get("link") or link_val.get("text", "")
            if url:
                return url
        elif isinstance(link_val, str) and link_val.startswith("http"):
            return link_val

    # Strategy 3: Query DISPATCH AITable for the PTS link by customer_name
    from core.config import get_settings
    settings = get_settings()
    if settings.dt_dispatch_base_id and settings.dt_dispatch_table_id and customer_name:
        try:
            records = await _cached_query_records(
                base_id=settings.dt_dispatch_base_id,
                table_id=settings.dt_dispatch_table_id,
            )
            if records:
                for rec in records:
                    rec_cells = rec.get("cells", {})
                    rec_name = extract_text(rec_cells.get(DISPATCH["客户名称"]))
                    if rec_name != customer_name:
                        continue
                    link_val = rec_cells.get(DISPATCH["巡检工单链接"])
                    if link_val:
                        if isinstance(link_val, dict):
                            url = link_val.get("link") or link_val.get("text", "")
                            if url:
                                return url
                        elif isinstance(link_val, str) and link_val.startswith("http"):
                            return link_val
        except Exception as e:
            logger.warning("Failed to query DISPATCH AITable for PTS URL: %s", e)

    return None


async def _write_back_dispatch_result(
    *,
    record_id: str,
    demand_id: str,
    order_id: str,
    base_id: str,
    table_id: str,
) -> None:
    """Write back demandId and orderId to the AITable record.

    - demandId → AntmbXo (需求编号)
    - orderId → LyTZGs5 (订单编号)
    """
    try:
        await dingtalk_client.update_records(
            records=[{
                "recordId": record_id,
                "cells": {
                    DISPATCH["需求编号"]: demand_id,
                    DISPATCH["订单编号"]: order_id,
                },
            }],
            base_id=base_id,
            table_id=table_id,
        )
        logger.info("Wrote back dispatch result to AITable: record=%s, demandId=%s, orderId=%s",
                     record_id, demand_id, order_id)
    except Exception as e:
        logger.error("Failed to write back dispatch result to AITable for record %s: %s", record_id, e)


async def _write_back_pts_url_missing(
    *,
    record_id: str,
    customer_name: str | None,
    base_id: str,
    table_id: str,
) -> None:
    """Write error marker to AITable when PTS URL is not found.

    Sets 需求编号 to a marker so the record is excluded from future polls,
    and adds a note to 备注 explaining the issue.
    """
    marker = f"PTS链接未找到: {customer_name or ''}"
    try:
        await dingtalk_client.update_records(
            records=[{
                "recordId": record_id,
                "cells": {
                    DISPATCH["需求编号"]: marker,
                    DISPATCH["备注"]: f"自动派单失败：未找到客户 {customer_name or ''} 的PTS工单链接，请手动处理",
                },
            }],
            base_id=base_id,
            table_id=table_id,
        )
        logger.info("Wrote PTS URL missing marker to AITable: record=%s", record_id)
        _invalidate_aitable_cache(base_id, table_id)
    except Exception as e:
        logger.error("Failed to write PTS URL missing marker to AITable for record %s: %s", record_id, e)


async def get_dispatch_pending(db: Session, count_only: bool = False) -> dict:
    """Return AITable records that meet dispatch conditions.

    Condition: 伙伴供应商 filled + 伙伴负责人 filled + 工程师 filled + 需求编号 empty
    """
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"pending": [], "total": 0}

    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )

    pending = []
    for record in records:
        record_id = record.get("recordId") or record.get("record_id", "")
        if not record_id:
            continue

        cells = record.get("cells", {})
        supplier = extract_select_name(cells.get(DISPATCH["伙伴供应商"]))
        partner_manager = extract_engineer(cells.get(DISPATCH["伙伴负责人"]))
        engineer = extract_engineer(cells.get(DISPATCH["工程师"]))
        demand_number = extract_text(cells.get(DISPATCH["需求编号"]))

        has_supplier = bool(supplier and supplier.strip())
        has_partner_manager = bool(partner_manager and partner_manager.strip())
        has_engineer = bool(engineer and engineer.strip())
        no_demand = not (demand_number and demand_number.strip())

        if not (has_supplier and has_partner_manager and has_engineer and no_demand):
            continue

        if count_only:
            pending.append({"record_id": record_id})
            continue

        # Find PTS URL (slow — skip in count_only mode)
        customer_name = extract_text(cells.get(DISPATCH["客户名称"]))
        product = extract_text(cells.get(DISPATCH["产品名称"]))
        dispatch_level = extract_select_name(cells.get(DISPATCH["派单等级"]))
        pts_url = await _find_pts_url(db, customer_name, cells)

        pending.append({
            "record_id": record_id,
            "customer_name": customer_name or "",
            "product": product or "",
            "supplier": supplier or "",
            "partner_manager": partner_manager or "",
            "engineer": engineer or "",
            "dispatch_level": dispatch_level or "",
            "pts_url": pts_url or "",
        })

    return {"pending": pending, "total": len(pending)}


async def trigger_manual_dispatch(db: Session, record_id: str) -> dict:
    """Manually trigger yunji dispatch for a specific AITable record."""
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"status": "error", "message": "Dispatch AITable not configured"}

    # Fetch the specific record
    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )

    target_record = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target_record = record
            break

    if not target_record:
        return {"status": "error", "message": f"Record {record_id} not found in AITable"}

    cells = target_record.get("cells", {})
    supplier = extract_select_name(cells.get(DISPATCH["伙伴供应商"]))
    engineer = extract_engineer(cells.get(DISPATCH["工程师"]))
    customer_name = extract_text(cells.get(DISPATCH["客户名称"]))

    if not supplier or not supplier.strip():
        return {"status": "error", "message": "伙伴供应商为空，无法派单"}

    if not engineer or not engineer.strip():
        return {"status": "error", "message": "工程师为空，无法派单"}

    # Find PTS URL
    pts_url = await _find_pts_url(db, customer_name, cells)
    if not pts_url:
        return {"status": "error", "message": f"未找到 {customer_name} 的 PTS 工单链接，无法派单"}

    # Execute dispatch
    from services.trigger_service import dispatch_from_aitable
    try:
        result = await dispatch_from_aitable(
            db,
            pts_url=pts_url,
            supplier=supplier,
            record_id=record_id,
            customer_name=customer_name,
        )

        if result.get("status") == "success":
            demand_id = result.get("demandId", "")
            order_id = result.get("orderId", "")

            # Write back to AITable
            await _write_back_dispatch_result(
                record_id=record_id,
                demand_id=demand_id,
                order_id=order_id,
                base_id=settings.dt_dispatch_base_id,
                table_id=settings.dt_dispatch_table_id,
            )
            # Invalidate dispatch cache so next query reflects the write-back
            _invalidate_aitable_cache(settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
            return {
                "status": "success",
                "demandId": demand_id,
                "orderId": order_id,
                "message": f"派单成功: demandId={demand_id}, orderId={order_id}",
            }
        else:
            return {
                "status": "failed",
                "message": result.get("message", "派单失败，未知原因"),
            }
    except Exception as e:
        logger.error("Manual dispatch failed for record %s: %s", record_id, e)
        return {"status": "error", "message": f"派单异常: {e}"}


# ── Email pending (客户巡检派单 table) ───────────────────────────────────


async def get_email_pending(db: Session) -> dict:
    """Return AITable records that meet email sending conditions.

    Condition: 邮件是否发送!='是' + 巡检报告 has attachment

    Results are cached for up to 2 hours (refreshed by scheduled probe).
    """
    global _email_pending_cache

    # Return cached result if fresh
    if _email_pending_cache is not None:
        ts, cached = _email_pending_cache
        if time.time() - ts < _EMAIL_CACHE_TTL:
            return cached
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"pending": [], "total": 0}

    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )

    pending = []
    for record in records:
        record_id = record.get("recordId") or record.get("record_id", "")
        if not record_id:
            continue

        cells = record.get("cells", {})
        report_attachments = cells.get(DISPATCH["巡检报告"])
        email_sent = extract_select_name(cells.get(DISPATCH["邮件是否发送"]))

        # Check conditions: has report + not yet sent
        has_report = isinstance(report_attachments, list) and len(report_attachments) > 0
        not_sent = not (email_sent and email_sent.strip() == "是")

        if not (has_report and not_sent):
            continue

        # Full data for display
        customer_name = extract_text(cells.get(DISPATCH["客户名称"]))
        product_name = extract_text(cells.get(DISPATCH["产品名称"]))
        report_email = extract_text(cells.get(DISPATCH["报告发送邮箱"]))
        sales_name = extract_text(cells.get(DISPATCH["销售"]))

        # Extract attachment info for display
        attachment_names = []
        if isinstance(report_attachments, list):
            attachment_names = [a.get("filename", "") for a in report_attachments if isinstance(a, dict)]

        # Parse email addresses from 报告发送邮箱 (separated by '、' or ',')
        email_list = []
        if report_email:
            for addr in report_email.replace("、", ",").replace("；", ",").split(","):
                addr = addr.strip()
                if addr and "@" in addr:
                    email_list.append(addr)

        pending.append({
            "record_id": record_id,
            "customer_name": customer_name or "",
            "product_name": product_name or "",
            "email_addresses": email_list,
            "email_address_str": ", ".join(email_list),
            "sales_name": sales_name or "",
            "attachments": attachment_names,
            "attachment_count": len(attachment_names),
        })

    result = {"pending": pending, "total": len(pending)}
    _email_pending_cache = (time.time(), result)
    return result


async def trigger_manual_email(db: Session, record_id: str, extra_emails: list[str] | None = None) -> dict:
    """Manually trigger email sending for a specific AITable record.

    Downloads the attachment from AITable, sends email, and writes back
    邮件是否发送 → '是' on success.

    Args:
        extra_emails: If provided, overrides the AITable 报告发送邮箱 field.
    """
    from core.config import get_settings
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"status": "error", "message": "AITable not configured"}

    # Fetch records and find the target
    records = await _cached_query_records(
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )

    target_record = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target_record = record
            break

    if not target_record:
        return {"status": "error", "message": f"Record {record_id} not found in AITable"}

    cells = target_record.get("cells", {})
    customer_name = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    product_name = extract_text(cells.get(DISPATCH["产品名称"])) or ""
    report_attachments = cells.get(DISPATCH["巡检报告"])
    email_addresses_str = extract_text(cells.get(DISPATCH["报告发送邮箱"])) or ""

    # Validate conditions
    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        return {"status": "error", "message": "巡检报告为空，无法发送邮件"}

    # Parse email addresses
    email_list = []
    if extra_emails:
        email_list = extra_emails
    elif email_addresses_str:
        for addr in email_addresses_str.replace("、", ",").replace("；", ",").split(","):
            addr = addr.strip()
            if addr and "@" in addr:
                email_list.append(addr)

    if not email_list:
        return {"status": "error", "message": "客户邮箱为空，请先填写收件人邮箱"}

    # Download attachments
    attachments = []
    download_errors = []
    for att in report_attachments:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename", "report")
        url = att.get("url", "")
        if not url:
            download_errors.append(f"{filename}: 无下载链接")
            continue
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                attachments.append((filename, resp.content))
                logger.info("Downloaded attachment: %s (%d bytes)", filename, len(resp.content))
        except Exception as e:
            download_errors.append(f"{filename}: {e}")
            logger.warning("Failed to download attachment %s: %s", filename, e)

    if not attachments and download_errors:
        return {
            "status": "error",
            "message": f"所有附件下载失败: {'; '.join(download_errors)}",
        }

    # Send email
    from services.trigger_service import email_from_aitable
    try:
        result = await email_from_aitable(
            db,
            record_id=record_id,
            customer_name=customer_name,
            product_name=product_name,
            email_addresses=email_list if email_list else None,
            attachments=attachments if attachments else None,
        )

        if result.get("status") == "success":
            # Write back 邮件是否发送 → '是' to AITable
            await _write_back_email_sent(
                record_id=record_id,
                base_id=settings.dt_dispatch_base_id,
                table_id=settings.dt_dispatch_table_id,
            )
            # Invalidate email cache so next request fetches fresh data
            _invalidate_email_cache()
            # Invalidate AITable query cache so next query reflects the write-back
            _invalidate_aitable_cache(settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
            msg = result.get("message", "邮件发送成功")
            if download_errors:
                msg += f" (部分附件下载失败: {'; '.join(download_errors)})"
            return {"status": "success", "message": msg}
        else:
            return {"status": "failed", "message": result.get("message", "邮件发送失败")}

    except Exception as e:
        logger.error("Manual email failed for record %s: %s", record_id, e)
        return {"status": "error", "message": f"邮件发送异常: {e}"}


async def _write_back_email_sent(
    *,
    record_id: str,
    base_id: str,
    table_id: str,
) -> None:
    """Write back 邮件是否发送='是' to the AITable record."""
    try:
        await dingtalk_client.update_records(
            records=[{
                "recordId": record_id,
                "cells": {
                    DISPATCH["邮件是否发送"]: "是",
                },
            }],
            base_id=base_id,
            table_id=table_id,
        )
        logger.info("Wrote back email_sent='是' to AITable: record=%s", record_id)
    except Exception as e:
        logger.error("Failed to write back email_sent to AITable for record %s: %s", record_id, e)


async def _broadcast_monitor(event_type: str, data: dict) -> None:
    """Broadcast a monitor event via WebSocket."""
    try:
        from apps.api.routers.ws import broadcaster

        await broadcaster.broadcast(event_type, data)
    except Exception:
        # Don't let broadcast failures affect monitor operations
        logger.debug("WebSocket broadcast failed for %s", event_type)


# ── Closure check (PTS work order auto-closure) ─────────────────────────────


async def run_closure_check(db: Session) -> dict:
    """Run the PTS work order closure check.

    Delegates to pts_closure_service for the actual closure logic.
    """
    from services.pts_closure_service import run_closure_check as _run_closure_check

    result = await _run_closure_check(db)
    await _broadcast_monitor("monitor.closure_check.completed", result)
    return result
