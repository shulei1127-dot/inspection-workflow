"""交付转售后回访闭环 API 路由"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(tags=["visit"])


class VisitRunResponse(BaseModel):
    status: str
    visit_log_id: str | None = None
    pts_visit_id: str | None = None
    visit_url: str | None = None
    reason: str | None = None
    error: str | None = None


class VisitBatchResponse(BaseModel):
    status: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = []


@router.get("/api/visit/pending")
async def list_visit_pending(force: bool = Query(False, description="强制刷新，跳过缓存")):
    """查看当前满足自动回访闭环条件的 AITable 记录。

    条件：回访状态=已回访 且 回访类型/满意度/备注不为空 且 回访链接为空
    默认 60 秒缓存，force=true 时强制重新查询 AITable。
    """
    from services.visit.visit_service import fetch_aitable_visit_pending

    pending = await fetch_aitable_visit_pending(force_refresh=force)
    return {
        "items": pending,
        "total": len(pending),
    }


@router.post("/api/visit/run/{record_id}", response_model=VisitRunResponse)
async def trigger_visit(record_id: str, db: Session = Depends(get_db)):
    """手动触发单条 AITable 记录的回访闭环。

    需要同时提供 delivery_id 等参数，或从 AITable 自动获取。
    """
    from services.visit.visit_service import fetch_aitable_visit_pending, run_visit_pipeline

    # 从 AITable 查找该记录
    pending = await fetch_aitable_visit_pending()
    record = next((r for r in pending if r["record_id"] == record_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="未找到满足回访条件的记录，可能回访状态非已回访或回访链接已存在")

    result = await run_visit_pipeline(
        db,
        record_id=record["record_id"],
        delivery_id=record["delivery_id"],
        customer_name=record.get("customer_name"),
        visit_type=record.get("visit_type"),
        satisfaction=record.get("satisfaction"),
        feedback_note=record.get("feedback_note"),
        region=record.get("region"),
        visit_owner=record.get("visit_owner"),
        trigger_source="manual",
    )
    return VisitRunResponse(
        status=result.get("status", "unknown"),
        visit_log_id=result.get("visit_log_id"),
        pts_visit_id=result.get("pts_visit_id"),
        visit_url=result.get("visit_url"),
        reason=result.get("reason"),
        error=result.get("error"),
    )


@router.post("/api/visit/batch", response_model=VisitBatchResponse)
async def batch_trigger_visits(db: Session = Depends(get_db)):
    """批量触发所有满足条件的回访闭环。"""
    from services.visit.visit_service import run_visit_batch

    result = await run_visit_batch(db, trigger_source="batch")
    return VisitBatchResponse(
        status=result.get("status", "unknown"),
        total=result.get("total", 0),
        completed=result.get("completed", 0),
        failed=result.get("failed", 0),
        skipped=result.get("skipped", 0),
        errors=result.get("errors", []),
    )


@router.post("/api/visit/retry/{visit_log_id}", response_model=VisitRunResponse)
async def retry_visit(visit_log_id: str, db: Session = Depends(get_db)):
    """重试失败的回访工单（从上次失败步骤恢复）。"""
    from services.visit.visit_service import get_visit_log, run_visit_pipeline

    visit_log = await get_visit_log(db, visit_log_id)
    if not visit_log:
        raise HTTPException(status_code=404, detail="回访日志不存在")

    if visit_log.status not in ("failed", "partial"):
        raise HTTPException(status_code=400, detail=f"回访状态为 {visit_log.status}，无需重试")

    result = await run_visit_pipeline(
        db,
        record_id=visit_log.dingtalk_record_id or "",
        delivery_id=visit_log.project_id,
        customer_name=visit_log.customer_name,
        trigger_source="retry",
    )
    return VisitRunResponse(
        status=result.get("status", "unknown"),
        visit_log_id=result.get("visit_log_id"),
        pts_visit_id=result.get("pts_visit_id"),
        visit_url=result.get("visit_url"),
        reason=result.get("reason"),
        error=result.get("error"),
    )


@router.get("/api/visit/logs")
async def list_visit_logs(
    status: str | None = Query(None, description="按状态筛选: pending/running/completed/failed/partial"),
    project_id: str | None = Query(None, description="按项目 ID 筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """查看回访日志列表。"""
    from apps.api.utils import fmt_cst
    from services.visit.visit_service import list_visit_logs as _list_logs

    items, total = await _list_logs(db, status=status, project_id=project_id, limit=limit, offset=offset)
    return {
        "items": [
            {
                "id": str(item.id),
                "project_id": item.project_id,
                "pts_visit_id": item.pts_visit_id,
                "project_name": item.project_name,
                "customer_name": item.customer_name,
                "region": item.region,
                "execution_mode": item.execution_mode,
                "status": item.status,
                "current_step": item.current_step,
                "visit_url": item.visit_url,
                "satisfaction_score": item.satisfaction_score,
                "visit_note": item.visit_note,
                "retry_count": item.retry_count,
                "error": item.error,
                "trigger_source": item.trigger_source,
                "step_fetch_delivery": item.step_fetch_delivery,
                "step_create_visit": item.step_create_visit,
                "step_find_visit": item.step_find_visit,
                "step_fill_feedback": item.step_fill_feedback,
                "step_finish_visit": item.step_finish_visit,
                "step_post_check": item.step_post_check,
                "step_dingtalk_writeback": item.step_dingtalk_writeback,
                "created_at": fmt_cst(item.created_at) if item.created_at else None,
                "updated_at": fmt_cst(item.updated_at) if item.updated_at else None,
            }
            for item in items
        ],
        "total": total,
    }


@router.get("/api/visit/{visit_log_id}")
async def get_visit_detail(visit_log_id: str, db: Session = Depends(get_db)):
    """查看单个回访工单详情。"""
    from apps.api.utils import fmt_cst
    from services.visit.visit_service import get_visit_log

    visit_log = await get_visit_log(db, visit_log_id)
    if not visit_log:
        raise HTTPException(status_code=404, detail="回访日志不存在")

    return {
        "id": str(visit_log.id),
        "project_id": visit_log.project_id,
        "review_log_id": str(visit_log.review_log_id) if visit_log.review_log_id else None,
        "pts_visit_id": visit_log.pts_visit_id,
        "project_name": visit_log.project_name,
        "customer_name": visit_log.customer_name,
        "region": visit_log.region,
        "execution_mode": visit_log.execution_mode,
        "status": visit_log.status,
        "current_step": visit_log.current_step,
        "step_fetch_delivery": visit_log.step_fetch_delivery,
        "step_create_visit": visit_log.step_create_visit,
        "step_find_visit": visit_log.step_find_visit,
        "step_fill_feedback": visit_log.step_fill_feedback,
        "step_finish_visit": visit_log.step_finish_visit,
        "step_post_check": visit_log.step_post_check,
        "step_dingtalk_writeback": visit_log.step_dingtalk_writeback,
        "company_id": visit_log.company_id,
        "visitor_id": visit_log.visitor_id,
        "contact_id": visit_log.contact_id,
        "product_id": visit_log.product_id,
        "form_id": visit_log.form_id,
        "content_id": visit_log.content_id,
        "visit_url": visit_log.visit_url,
        "satisfaction_score": visit_log.satisfaction_score,
        "visit_note": visit_log.visit_note,
        "dingtalk_record_id": visit_log.dingtalk_record_id,
        "dingtalk_writeback": visit_log.dingtalk_writeback,
        "error": visit_log.error,
        "retry_count": visit_log.retry_count,
        "step_log": visit_log.step_log,
        "trigger_source": visit_log.trigger_source,
        "created_at": fmt_cst(visit_log.created_at) if visit_log.created_at else None,
        "updated_at": fmt_cst(visit_log.updated_at) if visit_log.updated_at else None,
    }
