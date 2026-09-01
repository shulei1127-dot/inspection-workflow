"""巡检信息库服务：构建信息库 + 自动回写缺失的现场地址/报告邮箱。"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from models.inspection_info_library import InspectionInfoLibrary
from models.work_order import WorkOrder
from services import dingtalk_client
from services.aitable_fields import DISPATCH, extract_pts_order_id_from_link, extract_text

logger = logging.getLogger(__name__)

# AITable 字段ID → 信息库属性
_FIELD_TO_ATTR = {
    DISPATCH["巡检地址"]: "on_site_address",
    DISPATCH["报告发送邮箱"]: "report_email",
}


def _clean_text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _delivery_context(wo: WorkOrder | None) -> dict:
    """从工单 raw_data 提取交付/项目/联系人上下文。"""
    ctx = {
        "delivery_id": None,
        "project_id": None,
        "project_name": None,
        "contact_name": None,
        "contact_phone": None,
        "contact_email": None,
    }
    if wo is None:
        return ctx
    raw = wo.raw_data or {}
    delivery = raw.get("delivery") or {}
    ctx["delivery_id"] = delivery.get("id") or None
    project = delivery.get("project") or {}
    ctx["project_id"] = project.get("id") or None
    ctx["project_name"] = project.get("name") or None
    contact_list = delivery.get("contact_list")
    if isinstance(contact_list, list) and contact_list:
        first = contact_list[0].get("contact") if isinstance(contact_list[0], dict) else None
        if isinstance(first, dict):
            ctx["contact_name"] = first.get("name") or None
            ctx["contact_phone"] = first.get("phone") or None
            ctx["contact_email"] = first.get("email") or None
    return ctx


async def _query_dispatch_records() -> list[dict]:
    settings = get_settings()
    return await dingtalk_client.query_records(
        limit=200,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
        strict=True,
    )


async def sync_library(db: Session) -> dict:
    """从钉钉表 + 本地工单重建巡检信息库。"""
    records = await _query_dispatch_records()

    work_orders = db.execute(select(WorkOrder)).scalars().all()
    wo_by_pts = {wo.pts_order_id: wo for wo in work_orders}

    existing_rows = db.execute(select(InspectionInfoLibrary)).scalars().all()
    row_by_pts = {row.pts_order_id: row for row in existing_rows}

    new_count = updated_count = skipped = 0
    now = datetime.now(timezone.utc)
    for record in records:
        cells = record.get("cells") or {}
        pts_order_id = extract_pts_order_id_from_link(cells.get(DISPATCH["巡检工单链接"]))
        record_id = record.get("recordId") or record.get("record_id") or ""
        if not pts_order_id:
            skipped += 1
            continue

        wo = wo_by_pts.get(pts_order_id)
        ctx = _delivery_context(wo)

        customer_name = _clean_text(extract_text(cells.get(DISPATCH["客户名称"]))) or (wo.customer_name if wo else None)
        product_name = _clean_text(extract_text(cells.get(DISPATCH["产品名称"]))) or (wo.product_name if wo else None)
        address = _clean_text(extract_text(cells.get(DISPATCH["巡检地址"])))
        email = _clean_text(extract_text(cells.get(DISPATCH["报告发送邮箱"])))

        row = row_by_pts.get(pts_order_id)
        if row is None:
            row = InspectionInfoLibrary(pts_order_id=pts_order_id)
            db.add(row)
            row_by_pts[pts_order_id] = row  # 同一工单在钉钉表可能有多条月度记录，复用同一信息库行
            new_count += 1
        else:
            updated_count += 1

        row.delivery_id = ctx["delivery_id"] or row.delivery_id
        row.project_id = ctx["project_id"] or row.project_id
        row.project_name = ctx["project_name"] or row.project_name
        row.customer_name = customer_name or row.customer_name
        row.product_name = product_name or row.product_name
        row.contact_name = ctx["contact_name"] or row.contact_name
        row.contact_phone = ctx["contact_phone"] or row.contact_phone
        row.contact_email = ctx["contact_email"] or row.contact_email
        # 钉钉表为空时保留库内已有值（避免清掉已回写的数据）
        row.on_site_address = address or row.on_site_address
        row.report_email = email or row.report_email
        row.aitable_record_id = record_id or row.aitable_record_id
        row.last_synced_at = now

    db.commit()
    logger.info(
        "巡检信息库同步: 钉钉表=%d 新增=%d 已存在=%d 跳过=%d",
        len(records), new_count, updated_count, skipped,
    )
    return {"total": len(records), "new": new_count, "updated": updated_count, "skipped": skipped}


def _find_source(row_by_pts: dict[str, InspectionInfoLibrary], current: InspectionInfoLibrary, attr: str):
    """在同一 交付ID+项目ID 下，找最近更新且有该字段值的其他工单。"""
    best = None
    for candidate in row_by_pts.values():
        if candidate.pts_order_id == current.pts_order_id:
            continue
        if candidate.delivery_id != current.delivery_id or candidate.project_id != current.project_id:
            continue
        if not getattr(candidate, attr):
            continue
        candidate_ts = candidate.last_synced_at or candidate.updated_at
        best_ts = best.last_synced_at or best.updated_at if best else None
        if best is None or (candidate_ts and (best_ts is None or candidate_ts > best_ts)):
            best = candidate
    return best


async def backfill_missing_info(db: Session, dry_run: bool = False) -> dict:
    """把历史工单的现场地址/报告邮箱回写到同项目缺失字段的新工单。"""
    records = await _query_dispatch_records()

    library = db.execute(select(InspectionInfoLibrary)).scalars().all()
    by_pts = {row.pts_order_id: row for row in library}

    checked = filled = no_source = 0
    preview: list[dict] = []
    updates: list[dict] = []
    now = datetime.now(timezone.utc)

    for record in records:
        cells = record.get("cells") or {}
        pts_order_id = extract_pts_order_id_from_link(cells.get(DISPATCH["巡检工单链接"]))
        record_id = record.get("recordId") or record.get("record_id") or ""
        if not pts_order_id:
            continue

        current = by_pts.get(pts_order_id)
        if current is None or not current.delivery_id or not current.project_id:
            continue

        # 以钉钉表实际单元格为准判断缺失
        missing = {}
        for field_id, attr in _FIELD_TO_ATTR.items():
            if not _clean_text(extract_text(cells.get(field_id))):
                missing[field_id] = attr
        if not missing:
            continue

        checked += 1
        cells_update = {}
        sources = {}
        for field_id, attr in missing.items():
            source = _find_source(by_pts, current, attr)
            value = getattr(source, attr) if source else None
            if value:
                cells_update[field_id] = value
                sources[attr] = source.pts_order_id
        if not cells_update:
            no_source += 1
            continue

        preview.append({
            "record_id": record_id,
            "pts_order_id": pts_order_id,
            "customer_name": current.customer_name,
            "product_name": current.product_name,
            "filled_fields": [attr for attr in sources],
            "sources": sources,
            "address": cells_update.get(DISPATCH["巡检地址"]),
            "email": cells_update.get(DISPATCH["报告发送邮箱"]),
        })

        filled += 1
        if dry_run:
            continue

        updates.append({"recordId": record_id, "cells": cells_update})
        for field_id, attr in _FIELD_TO_ATTR.items():
            if field_id in cells_update:
                setattr(current, attr, cells_update[field_id])
        current.last_synced_at = now

    if updates:
        settings = get_settings()
        await dingtalk_client.update_records(
            records=updates,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
        )
    db.commit()
    logger.info(
        "巡检信息库回写: 检查=%d 已回写=%d 无来源=%d dry_run=%s",
        checked, filled, no_source, dry_run,
    )
    return {
        "checked": checked,
        "filled": filled,
        "no_source": no_source,
        "dry_run": dry_run,
        "preview": preview if dry_run else [],
    }
