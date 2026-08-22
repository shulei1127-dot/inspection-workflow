"""交付转售后审核 API 路由"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(tags=["review"])


class ReviewRunResponse(BaseModel):
    status: str
    total: int = 0
    passed: int = 0
    rejected: int = 0
    manual: int = 0
    errors: int = 0
    reason: str | None = None


class SingleAuditResponse(BaseModel):
    project_id: str
    project_name: str | None = None
    customer_name: str | None = None
    company_id: str | None = None
    crm_project_id: str | None = None
    sales_name: str | None = None
    sales_pts_id: str | None = None
    sales_lookup_status: str | None = None
    conclusion: str  # 通过 / 不通过 / 转人工审核 / error
    region: str | None = None
    delivery_type: str | None = None
    project_type: str | None = None
    rules: list[dict] | None = None
    dingtalk_writeback: dict | None = None
    pts_review_writeback: dict | None = None
    error: str | None = None


@router.post("/api/review/run", response_model=ReviewRunResponse)
async def trigger_review(db: Session = Depends(get_db)):
    """手动触发交付转售后审核流水线。"""
    from services.review.review_service import run_review_pipeline

    result = await run_review_pipeline(db, trigger_source="manual")
    return ReviewRunResponse(
        status=result.get("status", "unknown"),
        total=result.get("total", 0),
        passed=result.get("passed", 0),
        rejected=result.get("rejected", 0),
        manual=result.get("manual", 0),
        errors=result.get("errors", 0),
        reason=result.get("reason"),
    )


@router.post("/api/review/audit/{project_id}", response_model=SingleAuditResponse)
async def audit_single_project(
    project_id: str,
    skip_rules: str | None = Query(None, description="跳过的规则ID，逗号分隔，如 1"),
    db: Session = Depends(get_db),
):
    """对单个待审核项目执行审核。"""
    import logging
    from services.review.review_service import audit_single_project as _audit, _save_audit_log

    logger = logging.getLogger(__name__)

    # Parse skip_rules
    skip_set: set[int] | None = None
    if skip_rules:
        skip_set = {int(s.strip()) for s in skip_rules.split(",") if s.strip().isdigit()}

    result = await _audit(project_id, skip_rules=skip_set)

    # 保存审核日志
    try:
        _save_audit_log(
            db,
            project_id=project_id,
            project_name=result.get("project_name"),
            customer_name=result.get("customer_name"),
            result=result,
            trigger_source="manual_single",
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("单项目审核日志保存失败: project_id=%s", project_id)

    return SingleAuditResponse(
        project_id=result.get("project_id", project_id),
        project_name=result.get("project_name"),
        customer_name=result.get("customer_name"),
        company_id=result.get("company_id"),
        crm_project_id=result.get("crm_project_id"),
        sales_name=result.get("sales_name"),
        sales_pts_id=result.get("sales_pts_id"),
        sales_lookup_status=result.get("sales_lookup_status"),
        conclusion=result.get("conclusion", "error"),
        region=result.get("region"),
        delivery_type=result.get("delivery_type"),
        project_type=result.get("project_type"),
        rules=result.get("rules"),
        dingtalk_writeback=result.get("dingtalk_writeback"),
        pts_review_writeback=result.get("pts_review_writeback"),
        error=result.get("error"),
    )


@router.get("/api/review/logs")
async def list_review_logs(
    conclusion: str | None = Query(None, description="按结论筛选: 通过/不通过/转人工审核"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查看审核日志列表。"""
    from services.review.review_service import list_review_logs as _list_logs

    items, total = await _list_logs(db, conclusion=conclusion, limit=limit, offset=offset)
    return {
        "items": [
            {
                "id": str(item.id),
                "project_id": item.project_id,
                "project_name": item.project_name,
                "customer_name": item.customer_name,
                "conclusion": item.conclusion,
                "region": item.region,
                "delivery_type": item.delivery_type,
                "project_type": item.project_type,
                "rules_result": item.rules_result,
                "trigger_source": item.trigger_source,
                "error": item.error,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in items
        ],
        "total": total,
    }


@router.get("/api/review/pending")
async def list_pending_projects():
    """查看当前待审核项目列表（不执行审核）。"""
    from services.review.extractors.review_project_list import extract_pending_projects

    projects = await extract_pending_projects()
    return {
        "items": [
            {
                "project_id": p.project_id,
                "project_name": p.project_name,
                "customer_name": p.customer_name,
                "delivery_stage": p.delivery_stage,
                "stage_status": p.stage_status,
                "after_sales_leader": p.after_sales_leader,
                "assigner_name": p.assigner_name,
                "person_in_charge_name": p.person_in_charge_name,
            }
            for p in projects
        ],
        "total": len(projects),
    }
