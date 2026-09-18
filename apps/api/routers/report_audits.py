"""Inspection report audit API (read-only toward DingTalk and PTS)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.utils import fmt_cst
from core.db import get_db
from models.report_audit import ReportAudit
from services import report_audit_service

router = APIRouter(tags=["report-audits"])


def _serialize(row: ReportAudit) -> dict:
    return {
        "id": str(row.id),
        "aitable_record_id": row.aitable_record_id,
        "pts_order_id": row.pts_order_id,
        "customer_name": row.customer_name,
        "product_name": row.product_name,
        "filename": row.filename,
        "attachments": row.attachments or [],
        "rule_version": row.rule_version,
        "status": row.status,
        "score": row.score,
        "blocker_count": row.blocker_count,
        "error_count": row.error_count,
        "warning_count": row.warning_count,
        "findings": row.findings or [],
        "document_meta": row.document_meta or {},
        "llm_summary": row.llm_summary,
        "ai_used": row.ai_used,
        "error_message": row.error_message,
        "reviewed_at": fmt_cst(row.reviewed_at),
        "created_at": fmt_cst(row.created_at),
        "updated_at": fmt_cst(row.updated_at),
    }


@router.get("/api/report-audits/stats")
async def report_audit_stats(db: Session = Depends(get_db)):
    rows = db.query(ReportAudit.status, func.count(ReportAudit.id)).group_by(ReportAudit.status).all()
    counts = {status: count for status, count in rows}
    return {
        "total": sum(counts.values()),
        "passed": counts.get("passed", 0),
        "warning": counts.get("warning", 0),
        "rejected": counts.get("rejected", 0),
        "failed": counts.get("failed", 0),
        "running": counts.get("running", 0) + counts.get("pending", 0),
        "rule_version": report_audit_service.RULE_VERSION,
        "read_only": True,
    }


@router.get("/api/report-audits")
async def list_report_audits(
    status: str | None = Query(None),
    customer_name: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(ReportAudit)
    if status:
        query = query.filter(ReportAudit.status == status)
    if customer_name:
        query = query.filter(ReportAudit.customer_name.ilike(f"%{customer_name}%"))
    total = query.count()
    rows = query.order_by(ReportAudit.updated_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize(row) for row in rows]}


@router.post("/api/report-audits/scan")
async def scan_report_audits(
    limit: int = Query(3, ge=1, le=20),
    db: Session = Depends(get_db),
):
    return await report_audit_service.scan_reports(db, limit=limit)


@router.post("/api/report-audits/{audit_id}/recheck")
async def recheck_report_audit(audit_id: str, db: Session = Depends(get_db)):
    try:
        audit_uuid = uuid.UUID(audit_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid audit id") from exc
    row = db.query(ReportAudit).filter(ReportAudit.id == audit_uuid).first()
    if not row:
        raise HTTPException(status_code=404, detail="audit not found")
    result = await report_audit_service.scan_reports(db, limit=1, force_record_id=row.aitable_record_id)
    refreshed = db.query(ReportAudit).filter(ReportAudit.id == audit_uuid).first()
    return {"result": result, "audit": _serialize(refreshed or row)}
