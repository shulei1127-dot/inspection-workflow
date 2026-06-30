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
