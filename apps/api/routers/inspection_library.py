"""巡检信息库 API。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from apps.api.utils import fmt_cst
from core.db import get_db
from models.inspection_info_library import InspectionInfoLibrary
from services import inspection_library_service

router = APIRouter(tags=["inspection-library"])


def _serialize(row: InspectionInfoLibrary) -> dict:
    return {
        "pts_order_id": row.pts_order_id,
        "delivery_id": row.delivery_id,
        "project_id": row.project_id,
        "project_name": row.project_name,
        "customer_name": row.customer_name,
        "product_name": row.product_name,
        "contact_name": row.contact_name,
        "contact_phone": row.contact_phone,
        "contact_email": row.contact_email,
        "on_site_address": row.on_site_address,
        "report_email": row.report_email,
        "aitable_record_id": row.aitable_record_id,
        "updated_at": fmt_cst(row.updated_at),
        "last_synced_at": fmt_cst(row.last_synced_at),
    }


@router.get("/api/inspection-library")
async def list_inspection_library(
    customer_name: str | None = Query(None, description="按客户名称模糊筛选"),
    project_id: str | None = Query(None, description="按项目ID精确筛选"),
    delivery_id: str | None = Query(None, description="按交付ID精确筛选"),
    product_name: str | None = Query(None, description="按产品名称模糊筛选"),
    missing_info: bool = Query(False, description="只看缺少地址或邮箱的记录"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """巡检信息库列表（含筛选与分页）。"""
    q = db.query(InspectionInfoLibrary)
    if customer_name:
        q = q.filter(InspectionInfoLibrary.customer_name.ilike(f"%{customer_name}%"))
    if project_id:
        q = q.filter(InspectionInfoLibrary.project_id == project_id)
    if delivery_id:
        q = q.filter(InspectionInfoLibrary.delivery_id == delivery_id)
    if product_name:
        q = q.filter(InspectionInfoLibrary.product_name.ilike(f"%{product_name}%"))
    if missing_info:
        q = q.filter(or_(
            InspectionInfoLibrary.on_site_address.is_(None),
            InspectionInfoLibrary.on_site_address == "",
            InspectionInfoLibrary.report_email.is_(None),
            InspectionInfoLibrary.report_email == "",
        ))
    total = q.count()
    rows = q.order_by(InspectionInfoLibrary.updated_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize(row) for row in rows]}


@router.post("/api/inspection-library/sync")
async def sync_inspection_library(db: Session = Depends(get_db)):
    """从钉钉表 + 本地工单重建巡检信息库。"""
    return await inspection_library_service.sync_library(db)


@router.post("/api/inspection-library/backfill")
async def backfill_inspection_library(
    dry_run: bool = Query(False, description="true 仅预览不回写"),
    db: Session = Depends(get_db),
):
    """把同交付+同项目的历史地址/邮箱回写到缺失字段的记录。"""
    return await inspection_library_service.backfill_missing_info(db, dry_run=dry_run)
