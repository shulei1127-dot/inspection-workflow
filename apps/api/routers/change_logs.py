"""Change log knowledge-base endpoints."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from apps.api.utils import fmt_cst
from core.db import get_db
from models.change_log import ChangeLog
from services.change_log_service import (
    add_manual_entry,
    build_daily_summary,
    collect_git_changes,
    push_daily_summary,
)

router = APIRouter(tags=["change-logs"])


class ChangeLogCreateRequest(BaseModel):
    change_date: date
    category: str
    title: str
    detail: str | None = None
    author: str = ""


@router.get("/api/change-logs")
async def list_change_logs(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(ChangeLog)

    if date_from:
        query = query.filter(ChangeLog.change_date >= date_from)
    if date_to:
        query = query.filter(ChangeLog.change_date <= date_to)
    if category:
        query = query.filter(ChangeLog.category == category)
    if keyword:
        query = query.filter(
            or_(
                ChangeLog.title.ilike(f"%{keyword}%"),
                ChangeLog.detail.ilike(f"%{keyword}%"),
            )
        )

    total = query.count()
    rows = (
        query.order_by(ChangeLog.change_date.desc(), ChangeLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(row.id),
                "change_date": row.change_date.isoformat(),
                "source_type": row.source_type,
                "category": row.category,
                "title": row.title,
                "detail": row.detail,
                "git_hash": row.git_hash,
                "author": row.author,
                "pushed_to_dingtalk": row.pushed_to_dingtalk,
                "pushed_at": fmt_cst(row.pushed_at),
                "created_at": fmt_cst(row.created_at),
                "updated_at": fmt_cst(row.updated_at),
            }
            for row in rows
        ],
    }


@router.post("/api/change-logs")
async def create_change_log(req: ChangeLogCreateRequest, db: Session = Depends(get_db)):
    entry = add_manual_entry(
        db,
        change_date=req.change_date,
        category=req.category,
        title=req.title,
        detail=req.detail,
        author=req.author,
    )
    return {"status": "success", "id": str(entry.id)}


@router.delete("/api/change-logs/{entry_id}")
async def delete_change_log(entry_id: str, db: Session = Depends(get_db)):
    try:
        parsed_id = uuid.UUID(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid change log id") from exc

    row = db.query(ChangeLog).filter(ChangeLog.id == parsed_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entry not found")
    if row.source_type != "manual":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="only manual entries can be deleted")

    db.delete(row)
    db.commit()
    return {"status": "success"}


@router.post("/api/change-logs/collect")
async def collect_change_logs(target_date: date = Query(..., alias="date"), db: Session = Depends(get_db)):
    result = collect_git_changes(db, target_date)
    return {"status": "success", "result": result}


@router.post("/api/change-logs/push-daily")
async def push_daily_change_logs(
    target_date: date = Query(..., alias="date"),
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    result = await push_daily_summary(db, target_date, force=force)
    return {"status": "success", "result": result}


@router.get("/api/change-logs/summary")
async def get_change_log_summary(target_date: date = Query(..., alias="date"), db: Session = Depends(get_db)):
    return {"status": "success", "result": build_daily_summary(db, target_date)}
