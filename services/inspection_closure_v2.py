"""Idempotent inspection closure V2 coordinator.

V2 is isolated from the legacy closure implementation and selected only when
INSPECTION_CLOSURE_V2_ENABLED is true.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config import get_settings
from models.inspection_closure_attempt import InspectionClosureAttempt
from models.work_order import WorkOrder
from services import dingtalk_client, pts_client
from services.aitable_fields import DISPATCH, extract_pts_order_id_from_link, extract_select_name, extract_text

logger = logging.getLogger(__name__)

_TARGET_STAGE = "审核工单"
_CLOSED_STAGES = {_TARGET_STAGE, "结束", "已闭环"}
_MANUAL_ERROR_MARKERS = ("permission", "no permission", "无权限", "需要设置负责人", "负责人权限")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(/f/([^\)]+)\)")


class V2ConfigurationError(RuntimeError):
    """Raised when V2 cannot prove its configured schema is safe."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_id(record: dict) -> str:
    return str(record.get("recordId") or record.get("record_id") or "")


def _find_record(records: list[dict], record_id: str) -> dict | None:
    return next((record for record in records if _record_id(record) == record_id), None)


def _whitelist(settings) -> set[str]:
    return {
        value.strip()
        for value in str(getattr(settings, "inspection_closure_whitelist", "") or "").split(",")
        if value.strip()
    }


def _schema_fields(schema: dict | None) -> list[dict]:
    if not isinstance(schema, dict):
        return []
    data = schema.get("data", schema)
    if isinstance(data, dict):
        fields = data.get("fields") or data.get("fieldList")
        if isinstance(fields, list):
            return [field for field in fields if isinstance(field, dict)]
        tables = data.get("tables")
        if isinstance(tables, list):
            for table in tables:
                if isinstance(table, dict) and isinstance(table.get("fields"), list):
                    return [field for field in table["fields"] if isinstance(field, dict)]
        table = data.get("table")
        if isinstance(table, dict) and isinstance(table.get("fields"), list):
            return [field for field in table["fields"] if isinstance(field, dict)]
    return []


def _field_options(field: dict) -> set[str]:
    raw = field.get("options")
    if raw is None and isinstance(field.get("property"), dict):
        raw = field["property"].get("options")
    if raw is None and isinstance(field.get("config"), dict):
        raw = field["config"].get("options")
    if isinstance(raw, dict):
        raw = raw.get("options") or raw.get("choices") or raw.get("items")
    if not isinstance(raw, list):
        return set()
    names: set[str] = set()
    for option in raw:
        if isinstance(option, dict):
            name = option.get("name") or option.get("text") or option.get("label")
            if name:
                names.add(str(name))
        elif option:
            names.add(str(option))
    return names


async def validate_v2_configuration(settings=None) -> None:
    """Fail closed unless the configured AITable status field is real."""
    settings = settings or get_settings()
    if not getattr(settings, "dt_dispatch_base_id", "") or not getattr(settings, "dt_dispatch_table_id", ""):
        raise V2ConfigurationError("客户巡检派单 AITable 未配置")
    field_id = str(getattr(settings, "dt_dispatch_report_link_uploaded_field_id", "") or "").strip()
    if not field_id:
        raise V2ConfigurationError("未配置 Markdown链接报告是否上传 字段 ID")
    schema = await dingtalk_client.get_table(settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
    field = next(
        (
            item for item in _schema_fields(schema)
            if str(item.get("id") or item.get("fieldId") or item.get("field_id") or "") == field_id
        ),
        None,
    )
    if field is None:
        raise V2ConfigurationError(f"AITable 字段不存在: {field_id}")
    field_type = str(field.get("type") or field.get("fieldType") or "").lower()
    if field_type and field_type not in {"singleselect", "single_select", "select"}:
        raise V2ConfigurationError(f"AITable 字段类型不支持: {field_type}")
    options = _field_options(field)
    if not options:
        detail_schema = await dingtalk_client.get_field(
            settings.dt_dispatch_base_id,
            settings.dt_dispatch_table_id,
            field_ids=field_id,
        )
        detail = next(
            (
                item for item in _schema_fields(detail_schema)
                if str(item.get("id") or item.get("fieldId") or item.get("field_id") or "") == field_id
            ),
            None,
        )
        if detail:
            field = {**field, **detail}
            field_type = str(field.get("type") or field.get("fieldType") or "").lower()
            options = _field_options(field)
    if field_type and field_type not in {"singleselect", "single_select", "select"}:
        raise V2ConfigurationError(f"AITable 字段类型不支持: {field_type}")
    if not options or not {"是", "否"}.issubset(options):
        raise V2ConfigurationError("无法确认 Markdown链接报告字段的“是/否”选项")


def _attachment_source_id(attachment: dict) -> str:
    for key in ("id", "fileId", "attachmentId", "uid"):
        if attachment.get(key):
            return str(attachment[key])
    return ""


def _attachment_fingerprint(result: dict) -> str:
    source_id = result.get("source_id")
    if source_id:
        identity = {"source_id": source_id}
    elif result.get("sha256"):
        identity = {
            "sha256": result.get("sha256"),
            "filename": result.get("filename", ""),
            "size": result.get("size"),
        }
    else:
        identity = {"filename": result.get("filename", ""), "size": result.get("size")}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _safe_attachment_result(result: dict, source_id: str, original: dict) -> dict:
    safe = {
        "source_id": source_id,
        "filename": str(original.get("filename") or result.get("filename") or "report.pdf"),
        "size": result.get("size") or original.get("fileSize") or original.get("size"),
        "sha256": result.get("sha256"),
        "status": result.get("status", "failed"),
        "pts_file_id": result.get("pts_file_id"),
        "error": result.get("error"),
        "error_class": result.get("error_class"),
    }
    safe["fingerprint"] = _attachment_fingerprint(safe)
    return safe


def _report_fingerprint(results: list[dict]) -> str:
    values = sorted(item.get("fingerprint", "") for item in results)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _existing_links(details: dict | None) -> dict[str, str]:
    links: dict[str, str] = {}
    for info in (details or {}).get("info") or []:
        if not isinstance(info, dict) or not isinstance(info.get("note"), str):
            continue
        for filename, file_id in _LINK_RE.findall(info["note"]):
            links[filename] = file_id
    return links


def _is_manual_error(error: str) -> bool:
    lowered = error.lower()
    return any(marker.lower() in lowered for marker in _MANUAL_ERROR_MARKERS)


@contextmanager
def _record_lock(db: Session | None, pts_order_id: str, record_id: str) -> Iterator[None]:
    """Serialize overlapping API, scheduler, and email callbacks."""
    if db is None:
        yield
        return
    key = f"inspection-closure-v2:{record_id}:{pts_order_id}"
    advisory = False
    try:
        local = db.query(WorkOrder).filter(WorkOrder.pts_order_id == pts_order_id).with_for_update().first()
        if local is None:
            db.execute(text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"), {"lock_key": key})
            advisory = True
        yield
    finally:
        if advisory:
            try:
                db.execute(text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"), {"lock_key": key})
            except Exception:
                logger.exception("Failed to release closure advisory lock for %s", pts_order_id)


async def _prepare_attachments(attachments: list[dict]) -> tuple[list[dict], list[dict]]:
    prepared: list[dict] = []
    safe_results: list[dict] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        result = await pts_client.download_attachment_with_hash(attachment)
        safe = _safe_attachment_result(result, _attachment_source_id(attachment), attachment)
        prepared.append({"safe": safe, "content": result.get("content")})
        safe_results.append(safe)
    return prepared, safe_results


def _attempt_for(db: Session | None, record_id: str, pts_order_id: str, fingerprint: str):
    if db is None:
        return None
    return db.query(InspectionClosureAttempt).filter(
        InspectionClosureAttempt.aitable_record_id == record_id,
        InspectionClosureAttempt.pts_order_id == pts_order_id,
        InspectionClosureAttempt.report_fingerprint == fingerprint,
    ).first()


def _save_attempt(db: Session | None, attempt: InspectionClosureAttempt) -> None:
    if db is None:
        return
    db.add(attempt)
    db.commit()
    db.refresh(attempt)


def _get_or_create_attempt(db, record_id, pts_order_id, fingerprint, attachment_results, details):
    attempt = _attempt_for(db, record_id, pts_order_id, fingerprint)
    if attempt is None:
        attempt = InspectionClosureAttempt(
            id=uuid.uuid4(),
            aitable_record_id=record_id,
            pts_order_id=pts_order_id,
            report_fingerprint=fingerprint,
            coordinator_status="ELIGIBLE",
            attachment_results=attachment_results,
        )
        if db is not None:
            db.add(attempt)
    else:
        old = {item.get("fingerprint"): item for item in (attempt.attachment_results or []) if isinstance(item, dict)}
        attempt.attachment_results = [
            {**old.get(item.get("fingerprint"), {}), **item,
             "pts_file_id": old.get(item.get("fingerprint"), {}).get("pts_file_id") or item.get("pts_file_id")}
            for item in attachment_results
        ]
    stage = (details.get("current_stage") or {}).get("name", "")
    creator = details.get("creator") or {}
    claim_by = details.get("claim_by") or {}
    attempt.stage_before = attempt.stage_before or stage
    attempt.creator_id = creator.get("id") or attempt.creator_id
    attempt.creator_name = creator.get("name") or attempt.creator_name
    attempt.claim_by_id = claim_by.get("id") or attempt.claim_by_id
    attempt.claim_by_name = claim_by.get("name") or attempt.claim_by_name
    return attempt


async def _reconcile_reports(db, attempt, prepared, details, settings):
    existing = _existing_links(details)
    previous = {item.get("fingerprint"): item for item in (attempt.attachment_results or []) if isinstance(item, dict)}
    results: list[dict] = []
    missing_links: list[tuple[str, str]] = []
    dry_run = bool(getattr(settings, "inspection_closure_dry_run", True))
    upload_enabled = bool(getattr(settings, "inspection_closure_report_upload_enabled", False))
    attempt.coordinator_status = "REPORT_RECONCILING"
    attempt.attachment_results = []
    _save_attempt(db, attempt)

    for item in prepared:
        safe = dict(item["safe"])
        filename = safe["filename"]
        prior = previous.get(safe.get("fingerprint"), {})
        existing_file_id = existing.get(filename)
        file_id = existing_file_id or prior.get("pts_file_id") or safe.get("pts_file_id")
        if existing_file_id:
            safe.update(status="verified", pts_file_id=existing_file_id)
        elif file_id and prior.get("status") in {"uploaded", "verified"}:
            safe.update(status="uploaded", pts_file_id=file_id)
            missing_links.append((filename, file_id))
        elif safe.get("status") != "downloaded":
            safe.update(status="failed")
        elif dry_run:
            safe.update(status="planned_upload")
        elif not upload_enabled:
            safe.update(status="failed", error="报告上传开关未开启", error_class="permanent")
        else:
            upload_result = await pts_client.upload_file_via_api_with_retry(
                item.get("content") or b"", filename,
                max_retries=getattr(settings, "inspection_closure_upload_max_retries", 3),
                backoff_seconds=getattr(settings, "inspection_closure_retry_backoff_seconds", 2.0),
                max_backoff_seconds=getattr(settings, "inspection_closure_retry_max_backoff_seconds", 30.0),
            )
            safe.update(upload_result)
            if upload_result.get("pts_file_id"):
                safe["pts_file_id"] = upload_result["pts_file_id"]
                missing_links.append((filename, upload_result["pts_file_id"]))
            attempt.attachment_results = [*results, safe]
            _save_attempt(db, attempt)
        results.append(safe)

    attempt.attachment_results = results
    _save_attempt(db, attempt)
    if dry_run:
        attempt.coordinator_status = "DRY_RUN"
        attempt.last_error = "dry-run: 未执行远程 mutation"
        _save_attempt(db, attempt)
        return "dry_run", attempt.last_error

    failures = [item for item in results if item.get("status") not in {"verified", "uploaded"}]
    if failures:
        return "failed", f"附件处理失败: {failures[0].get('filename', 'unknown')}"

    if missing_links:
        note = f"巡检报告Markdown链接已补充（{len(results)}个附件）"
        note += "".join(f"\n[{name}](/f/{file_id})" for name, file_id in missing_links)
        try:
            await pts_client.add_work_order_info(details["id"], note=note, file_ids=None)
        except Exception:
            logger.warning("PTS note mutation failed for %s; re-querying", details.get("id"), exc_info=True)
        refreshed = await pts_client.query_work_order_details(details["id"])
        links = _existing_links(refreshed)
        if not all(
            links.get(str(item.get("filename") or "")) == item.get("pts_file_id")
            for item in results
        ):
            return "failed", "PTS Markdown链接写入或验证失败"
        details.clear()
        details.update(refreshed or {})

    attempt.coordinator_status = "REPORT_READY"
    attempt.last_error = None
    _save_attempt(db, attempt)
    return "ready", None


async def _reconcile_stage(db, attempt, details, settings):
    current = details
    before = (current.get("current_stage") or {}).get("name", "")
    attempt.stage_before = attempt.stage_before or before
    attempt.coordinator_status = "STAGE_RECONCILING"
    _save_attempt(db, attempt)
    if before in _CLOSED_STAGES or current.get("is_finished"):
        attempt.stage_after = before
        return "completed", None, current
    if not getattr(settings, "inspection_closure_stage_advance_enabled", False):
        return "report_ready", "阶段推进开关未开启", current

    max_attempts = max(1, int(getattr(settings, "inspection_closure_stage_max_attempts", 10)))
    assignee = getattr(settings, "inspection_closure_default_assignee_id", "")
    assignee_used = False
    for attempt_index in range(max_attempts):
        current = await pts_client.query_work_order_details(details["id"]) or current
        stage = (current.get("current_stage") or {}).get("name", "")
        if stage in _CLOSED_STAGES or current.get("is_finished"):
            attempt.stage_after = stage
            return "completed", None, current
        claim_by = assignee if stage == "指定工单负责人" and not assignee_used else None
        assignee_used = assignee_used or bool(claim_by)
        try:
            result = await pts_client.confirm_work_order_stage(details["id"], claim_by=claim_by)
        except Exception as exc:
            if _is_manual_error(str(exc)):
                return "manual", str(exc), current
            attempt.retry_count += 1
            attempt.last_error = str(exc)[:1000]
            attempt.error_class = "retryable"
            _save_attempt(db, attempt)
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(
                    getattr(settings, "inspection_closure_retry_max_backoff_seconds", 30.0),
                    getattr(settings, "inspection_closure_retry_backoff_seconds", 2.0) * (2 ** attempt_index),
                ))
            continue
        after = await pts_client.query_work_order_details(details["id"]) or current
        after_stage = (after.get("current_stage") or {}).get("name", "")
        if after_stage in _CLOSED_STAGES or after.get("is_finished"):
            attempt.stage_after = after_stage
            return "completed", None, after
        if result is False or result is None:
            attempt.retry_count += 1
            attempt.last_error = f"阶段推进返回 {result!r}，当前阶段为 {after_stage}"
            attempt.error_class = "retryable"
            _save_attempt(db, attempt)
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(
                    getattr(settings, "inspection_closure_retry_max_backoff_seconds", 30.0),
                    getattr(settings, "inspection_closure_retry_backoff_seconds", 2.0) * (2 ** attempt_index),
                ))
        current = after
    return "retryable_failed", "阶段推进重试次数耗尽", current


async def _write_back(db, attempt, record_id, *, report_uploaded, closed, settings) -> bool:
    if not getattr(settings, "inspection_closure_aitable_writeback_enabled", False):
        attempt.aitable_writeback_status = "disabled"
        _save_attempt(db, attempt)
        return True
    if getattr(settings, "inspection_closure_dry_run", True):
        attempt.aitable_writeback_status = "dry_run"
        _save_attempt(db, attempt)
        return True
    try:
        result = await dingtalk_client.update_records(
            records=[{"recordId": record_id, "cells": {
                settings.dt_dispatch_report_link_uploaded_field_id: report_uploaded,
                DISPATCH["工单是否闭环"]: closed,
            }}],
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
        )
        if result is None:
            raise RuntimeError("AITable writeback returned no result")
        records = await dingtalk_client.query_records(
            limit=100, base_id=settings.dt_dispatch_base_id, table_id=settings.dt_dispatch_table_id,
            fetch_all=True, strict=True,
        )
        verified = _find_record(records, record_id)
        cells = (verified or {}).get("cells", {})
        if extract_select_name(cells.get(settings.dt_dispatch_report_link_uploaded_field_id)) != report_uploaded:
            raise RuntimeError("AITable report writeback verification failed")
        if extract_select_name(cells.get(DISPATCH["工单是否闭环"])) != closed:
            raise RuntimeError("AITable closure writeback verification failed")
        attempt.aitable_writeback_status = "success"
        attempt.report_writeback_at = _now()
        attempt.closure_writeback_at = _now()
        _save_attempt(db, attempt)
        return True
    except Exception as exc:
        attempt.aitable_writeback_status = "pending"
        attempt.last_error = f"AITable 写回失败: {exc}"[:1000]
        attempt.error_class = "retryable"
        _save_attempt(db, attempt)
        return False


async def _notify_manual(db, attempt, details, reason, record, settings) -> None:
    stage = (details.get("current_stage") or {}).get("name", "")
    key = hashlib.sha256(f"{attempt.pts_order_id}:{stage}:{reason.strip().lower()}".encode()).hexdigest()
    if db is not None:
        existing = db.query(InspectionClosureAttempt).filter(
            InspectionClosureAttempt.aitable_record_id == attempt.aitable_record_id,
            InspectionClosureAttempt.pts_order_id == attempt.pts_order_id,
            InspectionClosureAttempt.manual_notification_key == key,
            InspectionClosureAttempt.manual_notification_status == "sent",
        ).first()
        if existing:
            return
    attempt.manual_notification_key = key
    if not getattr(settings, "inspection_closure_manual_notify_enabled", False):
        attempt.manual_notification_status = "disabled"
        _save_attempt(db, attempt)
        return
    from services.dingtalk_notifier import notify_closure_manual
    pts_url = f"https://pts.chaitin.net/project/order/{attempt.pts_order_id}"
    cells = record.get("cells") or {}
    customer = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    claim_by = (details.get("claim_by") or {}).get("name", "未设置")
    creator = (details.get("creator") or {}).get("name", "未知")
    sent = await notify_closure_manual(
        pts_order_id=attempt.pts_order_id,
        customer_name=customer,
        pts_url=pts_url,
        claim_by=claim_by,
        current_stage=stage,
        creator_name=creator,
        assignee_id=getattr(settings, "inspection_closure_default_assignee_id", ""),
        reason=reason,
    )
    attempt.manual_notification_status = "sent" if sent else "failed"
    attempt.manual_notified_at = _now() if sent else None
    _save_attempt(db, attempt)


async def coordinate_record(
    db: Session | None,
    record: dict,
    *,
    source: str = "scheduler",
    reports_only: bool = False,
) -> dict:
    settings = get_settings()
    record_id = _record_id(record)
    cells = record.get("cells") or {}
    pts_order_id = extract_pts_order_id_from_link(cells.get(DISPATCH["巡检工单链接"]))
    attachments = cells.get(DISPATCH["巡检报告"])
    if not record_id or not pts_order_id or not isinstance(attachments, list) or not attachments:
        return {"status": "skipped", "reason": "缺少记录 ID、PTS 链接或报告附件", "record_id": record_id}

    with _record_lock(db, pts_order_id, record_id):
        details = await pts_client.query_work_order_details(pts_order_id)
        if not details:
            return {"status": "retryable_failed", "reason": "PTS 工单查询失败", "record_id": record_id}
        prepared, safe_results = await _prepare_attachments(attachments)
        fingerprint = _report_fingerprint(safe_results)
        attempt = _get_or_create_attempt(db, record_id, pts_order_id, fingerprint, safe_results, details)
        if attempt.coordinator_status == "COMPLETED" and (
            not getattr(settings, "inspection_closure_aitable_writeback_enabled", False)
            or attempt.aitable_writeback_status == "success"
        ):
            return {"status": "completed", "record_id": record_id, "pts_order_id": pts_order_id, "idempotent": True}
        if getattr(settings, "inspection_closure_dry_run", True):
            attempt.coordinator_status = "DRY_RUN"
            attempt.last_error = "dry-run: 未执行远程 mutation"
            _save_attempt(db, attempt)
            return {"status": "dry_run", "record_id": record_id, "pts_order_id": pts_order_id, "source": source}

        report_status, report_error = await _reconcile_reports(db, attempt, prepared, details, settings)
        if report_status != "ready":
            await _write_back(db, attempt, record_id, report_uploaded="否", closed="否", settings=settings)
            attempt.coordinator_status = "RETRYABLE_FAILED" if report_status == "failed" else report_status.upper()
            attempt.last_error = report_error
            attempt.error_class = "retryable" if report_status == "failed" else "permanent"
            _save_attempt(db, attempt)
            return {"status": report_status, "record_id": record_id, "pts_order_id": pts_order_id, "message": report_error}

        if reports_only:
            writeback_ok = await _write_back(db, attempt, record_id, report_uploaded="是", closed="否", settings=settings)
            attempt.coordinator_status = "REPORT_READY" if writeback_ok else "WRITEBACK_PENDING"
            _save_attempt(db, attempt)
            return {"status": "report_ready" if writeback_ok else "writeback_pending", "record_id": record_id, "pts_order_id": pts_order_id}

        stage_status, stage_error, final_details = await _reconcile_stage(db, attempt, details, settings)
        if stage_status == "manual":
            await _write_back(db, attempt, record_id, report_uploaded="是", closed="否", settings=settings)
            await _notify_manual(db, attempt, final_details, stage_error or "需要人工推进", record, settings)
            attempt.coordinator_status = "MANUAL"
            attempt.last_error = stage_error
            _save_attempt(db, attempt)
            return {"status": "manual", "record_id": record_id, "pts_order_id": pts_order_id, "message": stage_error}
        if stage_status == "report_ready":
            await _write_back(db, attempt, record_id, report_uploaded="是", closed="否", settings=settings)
            attempt.coordinator_status = "REPORT_READY"
            attempt.last_error = stage_error
            _save_attempt(db, attempt)
            return {"status": "report_ready", "record_id": record_id, "pts_order_id": pts_order_id}
        if stage_status != "completed":
            await _write_back(db, attempt, record_id, report_uploaded="是", closed="否", settings=settings)
            attempt.coordinator_status = "RETRYABLE_FAILED"
            attempt.last_error = stage_error
            attempt.error_class = "retryable"
            _save_attempt(db, attempt)
            return {"status": "retryable_failed", "record_id": record_id, "pts_order_id": pts_order_id, "message": stage_error}

        writeback_ok = await _write_back(db, attempt, record_id, report_uploaded="是", closed="是", settings=settings)
        attempt.stage_after = (final_details.get("current_stage") or {}).get("name", _TARGET_STAGE)
        attempt.coordinator_status = "COMPLETED" if writeback_ok else "WRITEBACK_PENDING"
        attempt.completed_at = _now() if writeback_ok else None
        _save_attempt(db, attempt)
        wo = db.query(WorkOrder).filter(WorkOrder.pts_order_id == pts_order_id).first() if db is not None else None
        if db is not None and wo:
            wo.closure_status = "已闭环" if writeback_ok else "闭环中"
            db.commit()
        return {"status": "completed" if writeback_ok else "writeback_pending", "record_id": record_id, "pts_order_id": pts_order_id}


async def run_closure_check_v2(db: Session | None) -> dict:
    """Process all eligible dispatch-table records using V2."""
    settings = get_settings()
    try:
        await validate_v2_configuration(settings)
        records = await dingtalk_client.query_records(
            limit=200, base_id=settings.dt_dispatch_base_id, table_id=settings.dt_dispatch_table_id,
            fetch_all=True, strict=True,
        )
    except V2ConfigurationError as exc:
        logger.error("Inspection closure V2 configuration rejected: %s", exc)
        return {"status": "failed", "reason": str(exc), "configuration_error": True}
    except Exception as exc:
        logger.exception("Inspection closure V2 AITable query failed")
        return {"status": "failed", "reason": f"AITable 查询失败: {exc}"}

    whitelist = _whitelist(settings)
    eligible = []
    for record in records:
        record_id = _record_id(record)
        cells = record.get("cells") or {}
        email_sent = extract_select_name(cells.get(DISPATCH["邮件是否发送"]))
        closure = extract_select_name(cells.get(DISPATCH["工单是否闭环"]))
        reports = cells.get(DISPATCH["巡检报告"])
        if (not whitelist or record_id in whitelist) and email_sent == "是" and closure != "是" and isinstance(reports, list) and reports:
            eligible.append(record)

    summary = {"status": "success", "checked": len(eligible), "completed": 0, "manual": 0, "report_ready": 0, "retryable_failed": 0, "writeback_pending": 0, "dry_run": 0, "skipped": 0}
    for record in eligible:
        try:
            result = await coordinate_record(db, record, source="scheduler")
        except Exception:
            logger.exception("Inspection closure V2 failed for record %s", _record_id(record))
            result = {"status": "retryable_failed"}
        status = result.get("status", "retryable_failed")
        if status in summary and status not in {"status", "checked"}:
            summary[status] += 1
        elif status == "completed":
            summary["completed"] += 1
        elif status == "manual":
            summary["manual"] += 1
        elif status == "writeback_pending":
            summary["writeback_pending"] += 1
        elif status == "dry_run":
            summary["dry_run"] += 1
        else:
            summary["retryable_failed"] += 1
    return summary


async def coordinate_after_email_success(db: Session | None, record_id: str) -> dict:
    """Run one V2 attempt after an email sender durably writes success."""
    settings = get_settings()
    try:
        await validate_v2_configuration(settings)
        records = await dingtalk_client.query_records(
            limit=200, base_id=settings.dt_dispatch_base_id, table_id=settings.dt_dispatch_table_id,
            fetch_all=True, strict=True,
        )
    except Exception as exc:
        logger.warning("Immediate V2 closure skipped for %s: %s", record_id, exc)
        return {"status": "retryable_failed", "record_id": record_id, "message": str(exc)}
    record = _find_record(records, record_id)
    if not record:
        return {"status": "skipped", "record_id": record_id, "message": "AITable record not found"}
    cells = record.get("cells") or {}
    if extract_select_name(cells.get(DISPATCH["邮件是否发送"])) != "是":
        return {"status": "retryable_failed", "record_id": record_id, "message": "邮件成功状态尚未在 AITable 验证"}
    if extract_select_name(cells.get(DISPATCH["工单是否闭环"])) == "是":
        return {"status": "completed", "record_id": record_id, "message": "AITable 已标记闭环"}
    return await coordinate_record(db, record, source="email")
