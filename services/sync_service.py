"""Sync service: PTS work orders → local DB → DingTalk AITable.

Core flow:
1. PTS GraphQL query work orders for the month
2. Local dedup (pts_order_id)
3. Write to DingTalk AITable (via dws CLI)
4. Record sync_log
"""

import asyncio
import calendar
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config import get_settings
from models.sync_log import SyncLog
from models.work_order import WorkOrder
from models.work_order_sync import WorkOrderSync
from services import dingtalk_client, pts_client
from services.aitable_fields import (
    DISPATCH,
    COMPLETION_STAGES,
    current_month,
    extract_pts_order_id_from_link,
    extract_text,
)

logger = logging.getLogger(__name__)


def _is_completion_stage(raw: dict) -> bool:
    """Check if a PTS work order is in a completion stage (work already done)."""
    if raw.get("is_finished"):
        return True
    current_stage = raw.get("current_stage")
    if isinstance(current_stage, dict):
        stage_name = current_stage.get("name", "")
        if stage_name in COMPLETION_STAGES:
            return True
    return False


async def run_sync(
    db: Session,
    *,
    trigger_source: str = "manual",
    sync_month: str | None = None,
    push_to_aitable: bool = False,
    only_new_for_month: bool = False,
) -> SyncLog:
    """Run the PTS → local DB → AITable pipeline for one target month.

    ``WorkOrder`` remains unique by PTS ID, while ``WorkOrderSync`` owns the
    monthly AITable association.  This lets a delayed PTS order create a new
    AITable record in a later month without losing its historical records.
    """
    from apps.api.routers.ws import broadcaster

    sync_month = sync_month or current_month()
    if only_new_for_month:
        logger.debug("Monthly association mode supersedes legacy only_new_for_month")
    log = SyncLog(
        id=uuid.uuid4(),
        trigger_source=trigger_source,
        sync_month=sync_month,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()

    await broadcaster.broadcast("sync.started", {
        "trigger_source": trigger_source,
        "sync_month": sync_month,
    })

    try:
        raw_orders = await pts_client.query_inspection_work_orders(sync_month)
        log.fetched_count = len(raw_orders)
        logger.info("PTS fetched %d work orders for %s", len(raw_orders), sync_month)

        created_count = 0
        updated_count = 0
        skipped_count = 0
        pending_records: list[tuple[WorkOrder, WorkOrderSync]] = []

        for raw in raw_orders:
            pts_order_id = str(raw.get("id", ""))
            if not pts_order_id:
                continue

            existing = db.query(WorkOrder).filter(
                WorkOrder.pts_order_id == pts_order_id,
            ).first()
            is_completed = _is_completion_stage(raw)

            if is_completed:
                if existing:
                    if _data_changed(existing, raw):
                        _update_work_order(existing, raw)
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    wo = WorkOrder(
                        id=uuid.uuid4(),
                        pts_order_id=pts_order_id,
                        dt_sync_status="local_only",
                        dt_create_eligible=False,
                        closure_status="已闭环",
                        **_extract_fields(raw),
                    )
                    wo.raw_data = raw
                    db.add(wo)
                    created_count += 1
                continue

            if existing:
                if _data_changed(existing, raw):
                    _update_work_order(existing, raw)
                    updated_count += 1
                else:
                    skipped_count += 1
                wo = existing
            else:
                wo = WorkOrder(
                    id=uuid.uuid4(),
                    pts_order_id=pts_order_id,
                    dt_sync_status="pending",
                    dt_create_eligible=True,
                    **_extract_fields(raw),
                )
                wo.raw_data = raw
                db.add(wo)
                created_count += 1

            # The association is keyed by (PTS ID, target month), not by the
            # WorkOrder's legacy single-record columns.
            association = _prepare_monthly_candidate(db, wo, sync_month)
            if association is not None and association.create_eligible:
                pending_records.append((wo, association))

        db.commit()

        if push_to_aitable and pending_records:
            _acquire_dispatch_write_lock(db)
            aitable_lookup = await _build_dispatch_aitable_lookup()
            for wo, association in pending_records:
                try:
                    await _sync_to_aitable(
                        db,
                        wo,
                        sync_month=sync_month,
                        aitable_lookup=aitable_lookup,
                        sync_association=association,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to sync work order %s/%s to AITable: %s",
                        wo.pts_order_id,
                        sync_month,
                        e,
                    )
                    wo.dt_sync_status = "failed"
                    association.sync_status = "failed"
                    association.error_message = str(e)[:2000]
            db.commit()

        log.created_count = created_count
        log.updated_count = updated_count
        log.skipped_count = skipped_count
        log.status = "success"
        if push_to_aitable and any(
            association.sync_status == "failed"
            for _, association in pending_records
        ):
            log.status = "partial"
        if not push_to_aitable:
            log.status = "fetched_only"

    except Exception as e:
        logger.exception("Sync pipeline failed")
        log.status = "failed"
        log.error_message = str(e)[:2000]

    finally:
        log.completed_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except Exception:
            logger.exception("Failed to commit sync log final status")
            try:
                from core.db import SessionLocal as _SL
                with _SL() as _db2:
                    stale_log = _db2.query(SyncLog).filter(SyncLog.id == log.id).first()
                    if stale_log and stale_log.status == "running":
                        stale_log.status = log.status
                        stale_log.completed_at = log.completed_at
                        if log.error_message:
                            stale_log.error_message = log.error_message
                        _db2.commit()
            except Exception:
                logger.exception("Also failed to update sync log via fresh session")

    cleanup_stale_running_logs(db)

    await broadcaster.broadcast("sync.completed", {
        "status": log.status,
        "sync_month": log.sync_month,
        "fetched_count": log.fetched_count,
        "created_count": log.created_count,
        "updated_count": log.updated_count,
    })

    return log


async def push_to_aitable(db: Session, *, sync_month: str | None = None) -> dict:
    """Push eligible monthly associations to DingTalk AITable."""
    from apps.api.routers.ws import broadcaster

    sync_month = sync_month or current_month()
    parts = sync_month.split("-")
    year, month = int(parts[0]), int(parts[1])
    last_day = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    orders = db.query(WorkOrder).filter(
        WorkOrder.planned_completion >= start_date,
        WorkOrder.planned_completion <= end_date,
        WorkOrder.dt_create_eligible.is_(True),
    ).all()
    pending_records: list[tuple[WorkOrder, WorkOrderSync]] = []
    for wo in orders:
        association = _prepare_monthly_candidate(db, wo, sync_month)
        if association is not None and association.create_eligible:
            pending_records.append((wo, association))
    db.commit()

    if not pending_records:
        result = {
            "status": "success",
            "sync_month": sync_month,
            "pushed": 0,
            "failed": 0,
            "total": 0,
        }
        await broadcaster.broadcast("sync.pushed", result)
        return result

    try:
        _acquire_dispatch_write_lock(db)
        aitable_lookup = await _build_dispatch_aitable_lookup()
    except Exception as e:
        logger.error("AITable dedup query failed; aborting push: %s", e)
        return {
            "status": "error",
            "sync_month": sync_month,
            "pushed": 0,
            "failed": len(pending_records),
            "total": len(pending_records),
            "message": "AITable 去重查询失败，已中止推送以防止重复记录",
        }

    pushed = 0
    failed = 0
    for wo, association in pending_records:
        try:
            await _sync_to_aitable(
                db,
                wo,
                sync_month=sync_month,
                aitable_lookup=aitable_lookup,
                sync_association=association,
            )
            if association.sync_status == "synced":
                pushed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Failed to push work order %s to AITable: %s", wo.pts_order_id, e)
            wo.dt_sync_status = "failed"
            association.sync_status = "failed"
            association.error_message = str(e)[:2000]
            failed += 1

    db.commit()
    result = {
        "status": "success" if failed == 0 else "partial",
        "sync_month": sync_month,
        "pushed": pushed,
        "failed": failed,
        "total": len(pending_records),
    }
    await broadcaster.broadcast("sync.pushed", result)
    return result


def _acquire_dispatch_write_lock(db: Session) -> None:
    """Serialize dedup-query plus AITable writes across app instances."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {
        "key": "inspection-workflow:dispatch-aitable-write",
    })


def _month_from_value(value) -> str | None:
    """Extract a canonical YYYY-MM from a date-like value."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    if isinstance(value, dict):
        value = value.get("value") or value.get("date") or value.get("text") or value.get("iso")
    text_value = extract_text(value) or ""
    match = re.search(r"(\d{4})[-/]?(\d{2})", text_value)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def _record_sync_month(record: dict) -> str | None:
    cells = record.get("cells") or {}
    return _month_from_value(cells.get(DISPATCH["记录时间"]))


def _get_or_create_work_order_sync(db: Session, wo: WorkOrder, sync_month: str) -> WorkOrderSync:
    """Return the monthly association, creating it for deferred work orders."""
    association = db.query(WorkOrderSync).filter(
        WorkOrderSync.pts_order_id == wo.pts_order_id,
        WorkOrderSync.sync_month == sync_month,
    ).first()
    if association is None:
        association = WorkOrderSync(
            id=uuid.uuid4(),
            work_order_id=wo.id,
            pts_order_id=wo.pts_order_id,
            sync_month=sync_month,
            sync_status="pending",
            create_eligible=True,
        )
        db.add(association)
    return association


def _prepare_monthly_candidate(
    db: Session,
    wo: WorkOrder,
    sync_month: str,
) -> WorkOrderSync | None:
    """Prepare one target-month association without mutating old-month rows."""
    association = _get_or_create_work_order_sync(db, wo, sync_month)
    if association.sync_status == "synced" and association.aitable_record_id:
        wo.dt_record_id = association.aitable_record_id
        wo.dt_synced_month = sync_month
        wo.dt_create_eligible = False
        wo.dt_sync_status = "synced"
        return None

    association.create_eligible = True
    # Failed writes are retryable on the next sync. A previously synced
    # association is handled above and must not be recreated.
    if association.sync_status != "pending":
        association.sync_status = "pending"
    association.error_message = None
    wo.dt_record_id = association.aitable_record_id
    wo.dt_synced_month = sync_month
    wo.dt_create_eligible = True
    wo.dt_sync_status = "pending"
    return association


async def _build_dispatch_aitable_lookup() -> dict[Any, list[str]]:
    """Build a strict PTS order ID → AITable record IDs lookup.

    A missing or incomplete query is an error, not an empty table. This
    fail-closed behavior prevents a transient dws failure from turning an
    existing record into a new duplicate.
    """
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        raise RuntimeError("AITable dispatch base_id/table_id is not configured")

    records = await dingtalk_client.query_records(
        limit=1000,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
        strict=True,
    )

    lookup: dict[Any, list[str]] = {}
    link_field_id = DISPATCH["巡检工单链接"]
    for record in records:
        cells = record.get("cells", {})
        pts_id = extract_pts_order_id_from_link(cells.get(link_field_id))
        if not pts_id:
            continue
        record_id = record.get("recordId")
        if not record_id:
            raise RuntimeError(f"AITable record for PTS order {pts_id} has no recordId")
        month = _record_sync_month(record)
        if not month:
            raise RuntimeError(f"AITable record for PTS order {pts_id} has no valid sync month")
        lookup.setdefault((pts_id, month), []).append(record_id)

    duplicate_count = sum(1 for ids in lookup.values() if len(ids) > 1)
    logger.info(
        "Built DISPATCH AITable dedup lookup: %d pts_order_id mappings, %d duplicate conflicts",
        len(lookup),
        duplicate_count,
    )
    return lookup


async def _sync_to_aitable(
    db: Session | None,
    wo: WorkOrder,
    sync_month: str | None = None,
    aitable_lookup: dict[Any, list[str]] | None = None,
    sync_association: WorkOrderSync | None = None,
) -> None:
    """Create or reuse only the target month's AITable association."""
    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        raise RuntimeError("AITable dispatch base_id/table_id is not configured")

    target_month = sync_month or current_month()
    if sync_association is None and db is not None:
        sync_association = _get_or_create_work_order_sync(db, wo, target_month)
        if sync_association.sync_status == "synced" and sync_association.aitable_record_id:
            wo.dt_record_id = sync_association.aitable_record_id
            wo.dt_synced_month = target_month
            wo.dt_create_eligible = False
            wo.dt_sync_status = "synced"
            return

    if not wo.dt_create_eligible and not (
        sync_association is not None and sync_association.create_eligible
    ):
        logger.info("Skipping non-eligible AITable write for %s", wo.pts_order_id)
        return

    if aitable_lookup is None:
        aitable_lookup = await _build_dispatch_aitable_lookup()

    month_key = (wo.pts_order_id, target_month)
    if month_key in aitable_lookup:
        matches = aitable_lookup.get(month_key, [])
        lookup_key: object = month_key
    elif sync_month is None and wo.pts_order_id in aitable_lookup:
        # Keep the private helper's legacy call contract for V1/unit callers.
        matches = aitable_lookup.get(wo.pts_order_id, [])
        lookup_key = wo.pts_order_id
    else:
        matches = []
        lookup_key = wo.pts_order_id if sync_month is None else month_key

    if len(matches) > 1:
        wo.dt_sync_status = "dedup_conflict"
        if sync_association is not None:
            sync_association.sync_status = "dedup_conflict"
            sync_association.create_eligible = False
            sync_association.error_message = f"同月存在 {len(matches)} 条 AITable 记录"
        logger.error(
            "Refusing AITable write for %s/%s: %d records share the monthly PTS link (%s)",
            wo.pts_order_id,
            target_month,
            len(matches),
            ",".join(matches),
        )
        return

    if len(matches) == 1:
        record_id = matches[0]
        wo.dt_record_id = record_id
        wo.dt_create_eligible = False
        wo.dt_sync_status = "synced"
        wo.dt_synced_at = datetime.now(timezone.utc)
        wo.dt_synced_month = target_month
        if sync_association is not None:
            sync_association.aitable_record_id = record_id
            sync_association.create_eligible = False
            sync_association.sync_status = "synced"
            sync_association.synced_at = wo.dt_synced_at
            sync_association.error_message = None
        logger.info(
            "Dedup: reused AITable record %s for PTS order %s/%s; no create/update",
            record_id,
            wo.pts_order_id,
            target_month,
        )
        return

    result = await dingtalk_client.create_records(
        [{"cells": _work_order_to_cells(wo, sync_month=target_month)}],
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
    )
    if result is None:
        wo.dt_sync_status = "failed"
        if sync_association is not None:
            sync_association.sync_status = "failed"
            sync_association.error_message = "AITable create returned no result"
        logger.error("Failed to create AITable record for work order %s", wo.pts_order_id)
        return

    record_id = None
    if isinstance(result, dict):
        new_ids = result.get("newRecordIds", [])
        if isinstance(new_ids, list) and new_ids:
            record_id = new_ids[0]
    elif isinstance(result, list) and result and isinstance(result[0], dict):
        record_id = result[0].get("recordId") or result[0].get("record_id")

    if not record_id:
        wo.dt_sync_status = "failed"
        if sync_association is not None:
            sync_association.sync_status = "failed"
            sync_association.error_message = "AITable create returned no record ID"
        logger.error(
            "AITable create returned no record ID for work order %s, result=%s",
            wo.pts_order_id,
            str(result)[:200],
        )
        return

    now = datetime.now(timezone.utc)
    wo.dt_record_id = str(record_id)
    wo.dt_create_eligible = False
    wo.dt_sync_status = "synced"
    wo.dt_synced_at = now
    wo.dt_synced_month = target_month
    if sync_association is not None:
        sync_association.aitable_record_id = str(record_id)
        sync_association.create_eligible = False
        sync_association.sync_status = "synced"
        sync_association.synced_at = now
        sync_association.error_message = None
    aitable_lookup[lookup_key] = [str(record_id)]



def _extract_fields(raw: dict) -> dict:
    """Extract relevant fields from PTS raw data.

    PTS WorkOrder schema:
    - id: ID
    - type: [WorkOrderType!] (enum like expert_service__product_inspection)
    - company: { id, name }
    - claim_by: { id, name, username }
    - plan_complete_date: Time
    - is_finished: Boolean
    - desc: String
    - created_at: Time
    """
    # Map enum type values to Chinese display names
    type_display = {
        "expert_service__product_inspection": "产品巡检",
        "expert_service__product_duty": "增值服务",
        "expert_service__major_guarantee": "重大保障",
        "expert_service__log_analysis": "日志分析",
        "implementation_deployment__engine_and_rule_upgrade": "引擎升级",
        "company_service_plan": "公司服务计划",
        "other_types__other_types": "其他",
    }
    types = raw.get("type", [])
    type_names: list[str] = [str(type_display.get(t, t)) for t in types] if isinstance(types, list) else [str(types)]

    # Parse plan_complete_date (convert UTC to UTC+8 for China timezone)
    plan_date = None
    plan_str = raw.get("plan_complete_date")
    if plan_str:
        try:
            from datetime import datetime, timedelta
            utc_dt = datetime.fromisoformat(plan_str.replace("Z", "+00:00"))
            # PTS stores dates in UTC; convert to China time (UTC+8)
            cn_dt = utc_dt + timedelta(hours=8)
            plan_date = cn_dt.date()
        except (ValueError, TypeError):
            pass

    # After sale & delivery info
    delivery = raw.get("delivery")
    after_sale_name = None
    assigner_name = None
    contact_name = None
    contact_phone = None
    product_name = None
    crm_project_id = None
    if isinstance(delivery, dict):
        after_sale = delivery.get("after_sale")
        if isinstance(after_sale, dict):
            after_sale_name = after_sale.get("name")
        assigner = delivery.get("assigner")
        if isinstance(assigner, dict):
            assigner_name = assigner.get("name")
        # CRM project ID: delivery.project.id
        project = delivery.get("project")
        if isinstance(project, dict):
            crm_project_id = project.get("id")
        # Contact info: delivery.contact_list[].contact
        contact_list = delivery.get("contact_list")
        if isinstance(contact_list, list) and len(contact_list) > 0:
            first_contact = contact_list[0].get("contact") if isinstance(contact_list[0], dict) else None
            if isinstance(first_contact, dict):
                contact_name = first_contact.get("name")

            # AITable telephone field only accepts single valid phone number
            # Find first valid phone number from all contacts
            contact_phone = None
            for item in contact_list:
                if not isinstance(item, dict):
                    continue
                contact = item.get("contact")
                if not isinstance(contact, dict):
                    continue
                raw_phone = contact.get("phone")
                if not raw_phone:
                    continue
                # Split by Chinese comma (，) or English comma (,)
                for phone_part in raw_phone.replace("，", ",").split(","):
                    phone = phone_part.strip()
                    # Validate: must be at least 7 digits and not all same digit
                    if len(phone) >= 7 and phone.isdigit() and len(set(phone)) > 1:
                        contact_phone = phone
                        break
                if contact_phone:
                    break
        # Product info: delivery.product_info[].product_detail.product
        product_info = delivery.get("product_info")
        if isinstance(product_info, list) and len(product_info) > 0:
            names = []
            seen = set()
            for pi in product_info:
                detail = pi.get("product_detail") if isinstance(pi, dict) else None
                product = detail.get("product") if isinstance(detail, dict) else None
                if isinstance(product, dict) and product.get("name"):
                    pname = product["name"]
                    if pname not in seen:
                        seen.add(pname)
                        names.append(pname)
            product_name = "、".join(names) if names else None

    # Region mapping: assigner_name → region
    region = _map_person_to_region(assigner_name)

    # Sales: company.claim_by.name
    company = raw.get("company")
    sales_name = None
    if isinstance(company, dict):
        claim_by = company.get("claim_by")
        if isinstance(claim_by, dict):
            sales_name = claim_by.get("name")

    return {
        "pts_order_url": f"https://pts.chaitin.net/project/order/{raw.get('id', '')}",
        "order_type": "、".join(type_names),
        "customer_name": raw.get("company", {}).get("name") if isinstance(raw.get("company"), dict) else raw.get("customer_name"),
        "product_name": product_name,
        "engineer": raw.get("claim_by", {}).get("name") if isinstance(raw.get("claim_by"), dict) else raw.get("engineer"),
        "planned_completion": plan_date,
        "status": "已闭环" if raw.get("is_finished") else raw.get("current_stage", {}).get("name", "进行中") if isinstance(raw.get("current_stage"), dict) else "进行中",
        "after_sale": after_sale_name,
        "assigner_name": assigner_name,
        "region": region,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "sales_name": sales_name,
        "crm_project_id": crm_project_id,
    }


def _update_work_order(wo: WorkOrder, raw: dict) -> None:
    """Update work order fields from raw PTS data."""
    fields = _extract_fields(raw)
    for key, value in fields.items():
        setattr(wo, key, value)
    wo.raw_data = raw


def _data_changed(wo: WorkOrder, raw: dict) -> bool:
    """Check if PTS data has changed compared to local copy."""
    if wo.raw_data is None:
        return True
    # Compare key fields
    new_fields = _extract_fields(raw)
    for key, new_value in new_fields.items():
        old_value = getattr(wo, key, None)
        if str(old_value) != str(new_value):
            return True
    return False


# ── Region mapping: delivery.assigner.name → AITable 所属区域 ──────────────

_PERSON_TO_REGION: dict[str, str] = {}

_REGION_PERSONS = {
    "华东战区": "任嘉伟 鲍金鑫 高云松 郭林成 夏睿婷 陈欧翔 殷培源 宋健 陈祎雯 陈平远 卢占文 王瑞 黄瑞 廖雨田 田英超 杜文韬 黄彬 李东方",
    "华南战区": "唐政 李真真 万姚江 闫文军 叶利钢 彭明豪 兰廷灶 杜冠峥 郑思成 郑义全 黄泽孟 陈文超 邓智峰 邓万杰 吴冬兵 梁圣麟 雷昊",
    "华北东北战区": "田疆 王弸彪 黄诗琦 刘胜 王鑫裕 张镇朝 李京京 刘超 黄建朋 王均广",
    "西南西北战区": "饶君睿 罗果 黄科 欧阳凯 张强 柯李木 刘樊武 李升明 栗永顺 张明江",
    "金融头部战队": "张智国 贾腾辉 黄宏昊 孟祥宇 罗娇 侯伟 仇鑫杰 张嘉欣 刘春洋 邢凯迪 祝方正 郭文祥",
    "政府头部战队": "沈修阳 谢小倩 郑祖江 王德鑫 喻洁 周长良 庞临风",
    "通信头部战队": "王刚 田志强 田宇辰 王帅 王欣 张家祥",
    "华中战区": "李宁愿 万里秦 杨伦 刘腾 朱锟",
}

for _region, _names_str in _REGION_PERSONS.items():
    for _name in _names_str.split():
        _PERSON_TO_REGION[_name] = _region


def _map_person_to_region(person_name: str | None) -> str | None:
    """Map a person's name to their region/team."""
    if not person_name:
        return None
    return _PERSON_TO_REGION.get(person_name)


# AITable 所属区域 singleSelect option ID mapping — 日常增值服务进展 table
_REGION_TO_AITABLE_ID: dict[str, str] = {
    "华东战区": "sihv2JJ8oZ",
    "华北东北战区": "R3SaPQQ9ll",
    "华中战区": "o8qLHTdyLn",
    "西南西北战区": "YU38rRHTUD",
    "华南战区": "Dk7TGiNHfP",
    "金融头部战队": "rhNi44EXYy",
    "政府头部战队": "EERc6Y5oFU",
    "通信头部战队": "Ps8VlP32qE",
}

# AITable 所属区域 singleSelect option ID mapping — 客户巡检派单 table
# NOTE: DISPATCH 表的 所属区域 singleSelect 选项 ID 可能与 DAILY_SERVICE 不同，
# 需要通过 dws aitable table get 确认后更新
_DISPATCH_REGION_TO_AITABLE_ID: dict[str, str] = {
    "华东战区": "sihv2JJ8oZ",
    "华北东北战区": "R3SaPQQ9ll",
    "华中战区": "o8qLHTdyLn",
    "西南西北战区": "YU38rRHTUD",
    "华南战区": "Dk7TGiNHfP",
    "金融头部战队": "rhNi44EXYy",
    "政府头部战队": "EERc6Y5oFU",
    "通信头部战队": "Ps8VlP32qE",
}


def _work_order_to_cells(wo: WorkOrder, *, sync_month: str | None = None) -> dict:
    """Convert work order to AITable cells dict for 客户巡检派单 table.

    Field mapping (客户巡检派单 ← PTS):
    - 记录时间 ← 同步时写入当前日期
    - 客户名称 ← company.name
    - 产品名称 ← delivery.product_info 关联产品
    - 联系电话 ← delivery.contact_list 联系人电话
    - 工单类型 ← type (枚举 → 中文)
    - 巡检工单链接 ← pts_order_id → 工单链接
    - 所属区域 ← delivery.assigner → 人员→区域映射
    - 销售 ← company.claim_by.name

    All field IDs are defined in services.aitable_fields.DISPATCH.
    """
    cells = {}

    # 记录时间 is the first day of the target synchronization month.  It is
    # intentionally not the wall-clock date, so deferred orders stay grouped
    # under the month they are being synchronized into.
    target_month = sync_month or current_month()
    cells[DISPATCH["记录时间"]] = f"{target_month}-01"

    # 巡检工单链接 (url type)
    cells[DISPATCH["巡检工单链接"]] = f"https://pts.chaitin.net/project/order/{wo.pts_order_id}"

    if wo.customer_name:
        cells[DISPATCH["客户名称"]] = wo.customer_name
    if wo.product_name:
        cells[DISPATCH["产品名称"]] = wo.product_name
    if wo.order_type:
        cells[DISPATCH["工单类型"]] = wo.order_type
    if wo.contact_phone:
        cells[DISPATCH["联系电话"]] = wo.contact_phone
    if wo.region:
        # AITable singleSelect: write the display name, NOT the option ID
        cells[DISPATCH["所属区域"]] = wo.region
    if wo.sales_name:
        cells[DISPATCH["销售"]] = wo.sales_name
    if wo.crm_project_id:
        crm_url = f"https://crm.chaitin.net/project/{wo.crm_project_id}#base"
        cells[DISPATCH["CRM项目链接"]] = {"link": crm_url, "text": crm_url}

    return cells


async def adjust_planned_completion_to_month_end(
    db: Session,
    *,
    work_order_ids: list[str] | None = None,
    month: str | None = None,
) -> dict:
    """Adjust planned_completion to the last day of its month for selected work orders."""
    q = db.query(WorkOrder)

    if work_order_ids:
        uuids = []
        for wid in work_order_ids:
            try:
                uuids.append(uuid.UUID(wid))
            except ValueError:
                continue
        q = q.filter(WorkOrder.id.in_(uuids))
    elif month:
        parts = month.split("-")
        year, m = int(parts[0]), int(parts[1])
        last_day = calendar.monthrange(year, m)[1]
        start_date = date(year, m, 1)
        end_date = date(year, m, last_day)
        q = q.filter(
            WorkOrder.planned_completion >= start_date,
            WorkOrder.planned_completion <= end_date,
        )
    else:
        raise ValueError("Must provide work_order_ids or month")

    q = q.filter(WorkOrder.planned_completion_adjusted == False)  # noqa: E712
    orders = q.all()

    adjusted = 0
    skipped = 0

    for wo in orders:
        if wo.planned_completion is None:
            skipped += 1
            continue

        y, m = wo.planned_completion.year, wo.planned_completion.month
        last_day = calendar.monthrange(y, m)[1]
        new_date = date(y, m, last_day)

        if wo.planned_completion == new_date:
            skipped += 1
            continue

        wo.planned_completion = new_date
        wo.planned_completion_adjusted = True
        adjusted += 1

    db.commit()

    # Update PTS work orders' plan_complete_date
    pts_updated = 0
    pts_failed = 0
    if adjusted > 0:
        pts_updated, pts_failed = await _update_pts_planned_completion(orders)

    # Best-effort: update DAILY_SERVICE AITable records
    aitable_updated = 0
    aitable_failed = 0
    if adjusted > 0:
        aitable_updated, aitable_failed = await _update_aitable_planned_completion(db, orders)

    logger.info(
        "Adjusted planned_completion: %d adjusted, %d skipped, PTS %d updated / %d failed, AITable %d updated / %d failed",
        adjusted, skipped, pts_updated, pts_failed, aitable_updated, aitable_failed,
    )

    return {
        "status": "success",
        "adjusted": adjusted,
        "skipped": skipped,
        "pts_updated": pts_updated,
        "pts_failed": pts_failed,
        "aitable_updated": aitable_updated,
        "aitable_failed": aitable_failed,
    }


async def _update_pts_planned_completion(orders: list[WorkOrder]) -> tuple[int, int]:
    """Update PTS work orders' plan_complete_date to the last day of the month.

    Converts local date to UTC datetime string required by PTS API:
    Beijing time month last day 00:00 → UTC month last day minus 1 day 16:00.
    """
    from datetime import timedelta
    from services.pts_client import update_work_order_plan_complete_date

    updated = 0
    failed = 0

    # Only process orders that were actually adjusted
    adjusted_orders = [wo for wo in orders if wo.planned_completion_adjusted]
    if not adjusted_orders:
        return 0, 0

    for wo in adjusted_orders:
        if not wo.planned_completion or not wo.pts_order_id:
            continue

        # Convert: Beijing time last day 00:00 → UTC (subtract 8 hours)
        bj_last_day = wo.planned_completion  # already set to month last day
        utc_dt = datetime(bj_last_day.year, bj_last_day.month, bj_last_day.day, 0, 0, 0) - timedelta(hours=8)
        utc_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        success = await update_work_order_plan_complete_date(wo.pts_order_id, utc_str)
        if success:
            updated += 1
            logger.info("PTS plan_complete_date updated for %s → %s", wo.pts_order_id, utc_str)
        else:
            failed += 1
            logger.warning("PTS plan_complete_date update failed for %s", wo.pts_order_id)

        # Rate limit: max 5 requests per second per PTS token
        await asyncio.sleep(0.25)

    return updated, failed


async def _update_aitable_planned_completion(db: Session, orders: list[WorkOrder]) -> tuple[int, int]:
    """Best-effort update of planned_completion in DAILY_SERVICE AITable table."""
    from services.aitable_fields import DAILY_SERVICE

    settings = get_settings()
    if not settings.dt_aitable_base_id or not settings.dt_aitable_table_id:
        return 0, 0

    # Only process orders that were actually adjusted
    adjusted_orders = [wo for wo in orders if wo.planned_completion_adjusted]
    if not adjusted_orders:
        return 0, 0

    try:
        records = await dingtalk_client.query_records(
            limit=1000,
            base_id=settings.dt_aitable_base_id,
            table_id=settings.dt_aitable_table_id,
            fetch_all=True,
        )
    except Exception as e:
        logger.error("Failed to query DAILY_SERVICE AITable for planned_completion update: %s", e)
        return 0, len(adjusted_orders)

    # Build lookup: pts_order_id → record_id (using shared helper)
    aitable_lookup: dict[str, str] = {}
    for record in records:
        cells = record.get("cells", {})
        link_val = cells.get(DAILY_SERVICE.get("巡检工单链接", ""))
        pts_id = extract_pts_order_id_from_link(link_val)
        if pts_id:
            aitable_lookup[pts_id] = record.get("recordId", "")

    field_id = DAILY_SERVICE.get("工单计划完成时间")
    if not field_id:
        logger.warning("DAILY_SERVICE field ID for 工单计划完成时间 not found")
        return 0, len(adjusted_orders)

    updated = 0
    failed = 0

    for wo in adjusted_orders:
        if wo.pts_order_id not in aitable_lookup:
            failed += 1
            continue

        try:
            await dingtalk_client.update_records(
                [{
                    "recordId": aitable_lookup[wo.pts_order_id],
                    "cells": {field_id: wo.planned_completion.isoformat() if wo.planned_completion else ""},
                }],
                base_id=settings.dt_aitable_base_id,
                table_id=settings.dt_aitable_table_id,
            )
            updated += 1
        except Exception as e:
            logger.error("Failed to update AITable planned_completion for %s: %s", wo.pts_order_id, e)
            failed += 1

    return updated, failed


def cleanup_stale_running_logs(db: Session) -> None:
    """Mark stale 'running' sync logs as 'failed'.

    A sync log that has been 'running' for more than 10 minutes is
    considered stuck (from a crashed/restarted process) and is marked
    as 'failed' with an explanatory error message.

    Called at app startup and after each sync run.
    """
    from datetime import timedelta

    stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale_logs = db.query(SyncLog).filter(
        SyncLog.status == "running",
        SyncLog.started_at < stale_threshold,
    ).all()

    if not stale_logs:
        return

    for log in stale_logs:
        log.status = "failed"
        log.completed_at = datetime.now(timezone.utc)
        log.error_message = (log.error_message or "") + " [超时自动标记：进程可能已崩溃或重启]"

    db.commit()
    logger.warning("Marked %d stale 'running' sync logs as 'failed'", len(stale_logs))
