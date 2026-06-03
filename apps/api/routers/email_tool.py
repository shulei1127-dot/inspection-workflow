"""Email tool endpoints: upload PDF, AI extraction, send email."""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from core.db import get_db
from services.email_sender import send_email, extract_info_with_ai

logger = logging.getLogger(__name__)

router = APIRouter(tags=["email-tool"])

# In-memory store for uploaded files (per session)
# Key: file_id, Value: {filename, content, uploaded_at}
_upload_store: dict[str, dict] = {}


def _merge_multi_report_results(ai_infos: list[dict]) -> dict:
    """Merge AI extraction results from multiple PDF reports.

    Returns merged dict with: customer_name, product_name, quantity, emails,
    summary, summaries.
    """
    if not ai_infos:
        return {}

    if len(ai_infos) == 1:
        info = ai_infos[0]
        product = info.get("product_name", "产品")
        summary = info.get("summary", "")
        return {
            "customer_name": info.get("customer_name", ""),
            "product_name": product,
            "quantity": info.get("quantity", ""),
            "emails": info.get("emails", []),
            "summary": summary,
            "summaries": [{"product": product, "summary": summary}],
        }

    # Multiple reports: merge with same logic as Streamlit
    customer_name = ""
    product_names = []
    products_with_quantity = []
    all_emails = []
    summaries = []

    for info in ai_infos:
        # customer_name: take first non-empty
        if not customer_name and info.get("customer_name"):
            customer_name = info["customer_name"]

        prod = info.get("product_name", "")
        if prod:
            product_names.append(prod)

        qty = info.get("quantity", "")
        if qty and prod:
            products_with_quantity.append(f"{qty}{prod}")
        elif prod:
            products_with_quantity.append(prod)

        if info.get("emails"):
            all_emails.extend(info["emails"])

        summaries.append({
            "product": prod or "产品",
            "summary": info.get("summary", ""),
        })

    # summary: single report → direct; multi → segmented by product
    summary = "\n\n".join(
        f"【{s['product']}】\n{s['summary']}" for s in summaries if s["summary"]
    )

    return {
        "customer_name": customer_name,
        "product_name": "、".join(product_names) if product_names else "",
        "quantity": "、".join(products_with_quantity) if products_with_quantity else "",
        "emails": list(dict.fromkeys(all_emails)),  # dedupe, preserve order
        "summary": summary,
        "summaries": summaries,
    }


@router.post("/api/email-tool/extract")
async def extract_pdf_info(files: list[UploadFile] = File(...)):
    """Upload PDF files and extract info via AI.

    Returns extracted info for each file, plus stores file content for later sending.
    """
    import fitz  # PyMuPDF

    results = []
    all_emails = []

    for f in files:
        content = await f.read()
        file_id = str(uuid.uuid4())

        # Store for later sending
        _upload_store[file_id] = {
            "filename": f.filename or "report.pdf",
            "content": content,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }

        # Extract text from PDF
        info = None
        ai_error = None
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()

            if text.strip():
                info, ai_error = extract_info_with_ai(text)
                if info and info.get("emails"):
                    all_emails.extend(info["emails"])
            else:
                ai_error = "PDF 文本为空，无法提取信息"
        except Exception as e:
            ai_error = f"PDF 解析失败: {e}"

        results.append({
            "file_id": file_id,
            "filename": f.filename,
            "info": info or {
                "customer_name": "",
                "product_name": "",
                "inspection_date": "",
                "quantity": "",
                "emails": [],
                "summary": "",
            },
            "ai_error": ai_error,
        })

    return {"files": results, "all_emails": list(set(all_emails))}


@router.post("/api/email-tool/re-extract")
async def re_extract_pdf_info(file_ids: str = Form(...)):
    """Re-run AI extraction on already-stored files (by file_ids).

    Used when files were fetched from AITable (no raw upload).
    """
    import fitz  # PyMuPDF

    ids = [fid.strip() for fid in file_ids.split(",") if fid.strip()]
    if not ids:
        return {"files": [], "all_emails": []}

    results = []
    all_emails = []

    for fid in ids:
        stored = _upload_store.get(fid)
        if not stored:
            results.append({
                "file_id": fid,
                "filename": "",
                "info": {
                    "customer_name": "",
                    "product_name": "",
                    "inspection_date": "",
                    "quantity": "",
                    "emails": [],
                    "summary": "",
                },
                "ai_error": f"文件未找到: {fid}",
            })
            continue

        content = stored["content"]
        filename = stored["filename"]
        info = None
        ai_error = None
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()

            if text.strip():
                info, ai_error = extract_info_with_ai(text)
                if info and info.get("emails"):
                    all_emails.extend(info["emails"])
            else:
                ai_error = "PDF 文本为空，无法提取信息"
        except Exception as e:
            ai_error = f"PDF 解析失败: {e}"

        results.append({
            "file_id": fid,
            "filename": filename,
            "info": info or {
                "customer_name": "",
                "product_name": "",
                "inspection_date": "",
                "quantity": "",
                "emails": [],
                "summary": "",
            },
            "ai_error": ai_error,
        })

    return {"files": results, "all_emails": list(set(all_emails))}


@router.post("/api/email-tool/convert-name")
async def convert_name_to_email(name: str = Form(...)):
    """Convert a Chinese name to chaitin.com email address."""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return {"email": name if "@" in name else ""}

    # Compound surnames
    COMPOUND_SURNAMES = {
        "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫",
        "诸葛", "尉迟", "皇甫", "公孙", "慕容", "长孙", "宇文", "司徒",
    }

    import re
    normalized = re.sub(r"\s+", "", name)

    if not normalized or not re.search(r"[\u4e00-\u9fff]", normalized) or "@" in normalized:
        return {"email": normalized}

    if not re.fullmatch(r"[\u4e00-\u9fff]+", normalized) or len(normalized) < 2:
        return {"email": normalized}

    surname_len = 2 if len(normalized) > 2 and normalized[:2] in COMPOUND_SURNAMES else 1
    surname = normalized[:surname_len]
    given_name = normalized[surname_len:]

    if not given_name:
        return {"email": normalized}

    surname_py = "".join(lazy_pinyin(surname))
    given_py = "".join(lazy_pinyin(given_name))
    email = f"{given_py}.{surname_py}@chaitin.com".lower()

    return {"email": email}


@router.get("/api/email-tool/fetch-aitable-attachments/{record_id}")
async def fetch_aitable_attachments(record_id: str):
    """Download attachments from AITable record and store in upload store.

    Returns file_ids that can be used with the send endpoint.
    """
    from services import dingtalk_client
    from services.aitable_fields import DISPATCH, extract_text, extract_select_name
    from core.config import get_settings

    settings = get_settings()
    if not settings.dt_dispatch_base_id or not settings.dt_dispatch_table_id:
        return {"success": False, "message": "AITable 未配置"}

    try:
        records = await dingtalk_client.query_records(
            limit=100,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
            fetch_all=True,
        )
    except Exception as e:
        return {"success": False, "message": f"查询 AITable 失败: {e}"}

    target = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target = record
            break

    if not target:
        return {"success": False, "message": f"未找到记录 {record_id}"}

    cells = target.get("cells", {})
    report_attachments = cells.get(DISPATCH["巡检报告"])
    customer_name = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    product_name = extract_text(cells.get(DISPATCH["产品名称"])) or ""
    email_addresses = extract_text(cells.get(DISPATCH["报告发送邮箱"])) or ""
    sales_name = extract_text(cells.get(DISPATCH["销售"])) or ""

    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        return {"success": False, "message": "该记录无巡检报告附件"}

    # Download and store attachments
    import httpx
    file_ids = []
    filenames = []
    download_errors = []

    for att in report_attachments:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename", "report.pdf")
        url = att.get("url", "")
        if not url:
            download_errors.append(f"{filename}: 无下载链接")
            continue
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                file_id = str(uuid.uuid4())
                _upload_store[file_id] = {
                    "filename": filename,
                    "content": resp.content,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                }
                file_ids.append(file_id)
                filenames.append(filename)
                logger.info("Fetched AITable attachment: %s (%d bytes)", filename, len(resp.content))
        except Exception as e:
            download_errors.append(f"{filename}: {e}")
            logger.warning("Failed to download attachment %s: %s", filename, e)

    if not file_ids:
        return {"success": False, "message": f"所有附件下载失败: {'; '.join(download_errors)}"}

    # Parse email addresses (already from DISPATCH table, no separate lookup needed)
    email_list = []
    if email_addresses:
        for addr in email_addresses.replace("、", ",").replace("；", ",").split(","):
            addr = addr.strip()
            if addr and "@" in addr:
                email_list.append(addr)

    # Try AI extraction on ALL downloaded PDFs (multi-report support)
    ai_infos = []
    all_ai_errors = []
    import fitz
    for fid in file_ids:
        content = _upload_store[fid]["content"]
        filename = _upload_store[fid]["filename"]
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            if text.strip():
                info, ai_error = extract_info_with_ai(text)
                if info:
                    info["_filename"] = filename
                    ai_infos.append(info)
                if ai_error:
                    all_ai_errors.append(f"{filename}: {ai_error}")
            else:
                all_ai_errors.append(f"{filename}: PDF 文本为空")
        except Exception as e:
            all_ai_errors.append(f"{filename}: PDF 解析失败: {e}")

    # Merge multi-report results
    merged = _merge_multi_report_results(ai_infos)

    result = {
        "success": True,
        "file_ids": file_ids,
        "filenames": filenames,
        "customer_name": merged.get("customer_name") or customer_name,
        "product_name": merged.get("product_name") or product_name,
        "email_addresses": list(set(email_list + merged.get("emails", []))),
        "sales_name": sales_name,
    }
    result["email_address_str"] = ", ".join(result["email_addresses"])

    if ai_infos:
        result["ai_info"] = ai_infos[0] if len(ai_infos) == 1 else merged
        result["ai_infos"] = ai_infos
    if merged.get("summaries"):
        result["summaries"] = merged["summaries"]
        result["summary"] = merged["summary"]
    if all_ai_errors:
        result["ai_error"] = "; ".join(all_ai_errors)
    if download_errors:
        result["download_errors"] = download_errors

    return result

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "email_tool" / "data"
_DATA_DIR.mkdir(exist_ok=True)
_LOCAL_CONFIG_FILE = _DATA_DIR / "config.json"
_HISTORY_FILE = _DATA_DIR / "history.json"


def _load_local_config() -> dict:
    if _LOCAL_CONFIG_FILE.exists():
        with open(_LOCAL_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_local_config(cfg: dict) -> None:
    with open(_LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _load_history() -> list:
    if _HISTORY_FILE.exists():
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_history(hist: list) -> None:
    with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


@router.post("/api/email-tool/save-config")
async def save_email_config(config: dict):
    """Save email config to local config.json (overrides .env values)."""
    _save_local_config(config)
    return {"success": True}


@router.get("/api/email-tool/config")
async def get_email_config():
    """Return current email SMTP config (for display, password masked)."""
    local_cfg = _load_local_config()
    from core.config import get_settings
    settings = get_settings()
    return {
        "sender_email": local_cfg.get("sender_email") or settings.inspection_email_sender or "",
        "sender_name": local_cfg.get("sender_name", "长亭科技"),
        "smtp_server": local_cfg.get("smtp_server") or settings.inspection_email_smtp_host,
        "smtp_port": local_cfg.get("smtp_port") or settings.inspection_email_smtp_port,
        "has_password": bool(local_cfg.get("sender_password") or settings.inspection_email_password),
        "cc_emails": local_cfg.get("cc_emails", ""),
    }


@router.get("/api/email-tool/history")
async def get_email_history():
    """Return last 20 send history records."""
    return {"history": _load_history()[:20]}


@router.post("/api/email-tool/send")
async def send_inspection_email(
    to_emails: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    file_ids: str = Form(default=""),
    cc_emails: str = Form(default=""),
    record_id: str = Form(default=""),
    customer: str = Form(default=""),
    product: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Send inspection email with attachments."""
    import smtplib
    from email.header import Header
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr

    recipients = [e.strip() for e in to_emails.replace("，", ",").split(",") if e.strip() and "@" in e]
    if not recipients:
        return {"success": False, "message": "收件人邮箱为空"}

    cc_list = [e.strip() for e in cc_emails.replace("，", ",").split(",") if e.strip() and "@" in e] if cc_emails else []

    # Collect attachments from upload store
    attachments = []
    if file_ids:
        for fid in file_ids.split(","):
            fid = fid.strip()
            stored = _upload_store.get(fid)
            if stored:
                attachments.append((stored["filename"], stored["content"]))

    # Load config: local config.json overrides .env
    local_cfg = _load_local_config()
    from core.config import get_settings
    settings = get_settings()

    sender_email = local_cfg.get("sender_email") or settings.inspection_email_sender
    sender_password = local_cfg.get("sender_password") or settings.inspection_email_password
    sender_name = local_cfg.get("sender_name", "长亭科技")
    smtp_server = local_cfg.get("smtp_server") or settings.inspection_email_smtp_host
    smtp_port = int(local_cfg.get("smtp_port") or settings.inspection_email_smtp_port)

    if not sender_email or not sender_password:
        return {"success": False, "message": "请先配置发件人邮箱和授权码"}

    # Build email
    try:
        msg = MIMEMultipart()
        msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_email))
        msg["To"] = ", ".join(recipients)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Subject"] = Header(subject, "utf-8")
        msg.attach(MIMEText(body, "plain", "utf-8"))

        for att_name, att_bytes in attachments:
            att = MIMEApplication(att_bytes)
            att.add_header("Content-Disposition", "attachment", filename=att_name)
            msg.attach(att)

        all_recipients = recipients + cc_list
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())

        success, message = True, "发送成功"
    except Exception as e:
        success, message = False, f"发送失败: {e}"

    # Record history
    history = _load_history()
    history.insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "to": to_emails,
        "customer": customer,
        "product": product,
        "files": len(attachments),
        "status": "成功" if success else "失败",
    })
    _save_history(history[:50])

    # If send was successful and record_id was provided, write back 邮件是否发送='是' to AITable
    closure_result = None
    if success and record_id:
        try:
            from services.monitor_service import _write_back_email_sent, _invalidate_email_cache, _invalidate_aitable_cache
            from core.config import get_settings as _get_settings
            _settings = _get_settings()
            await _write_back_email_sent(
                record_id=record_id,
                base_id=_settings.dt_dispatch_base_id,
                table_id=_settings.dt_dispatch_table_id,
            )
            _invalidate_email_cache()
            _invalidate_aitable_cache(_settings.dt_dispatch_base_id, _settings.dt_dispatch_table_id)
        except Exception as e:
            logger.warning("Failed to write back email_sent to AITable: %s", e)

        # Auto-close the corresponding work order
        try:
            from services.pts_closure_service import close_work_order_after_email
            closure_result = await close_work_order_after_email(db, record_id)
            logger.info("Auto-closure result for record %s: %s", record_id, closure_result)
        except Exception as e:
            logger.warning("Auto-closure failed for record %s: %s", record_id, e)
            closure_result = {"success": False, "message": f"闭环异常: {e}"}

        # Update WorkOrder email status in local DB
        try:
            from models.work_order import WorkOrder
            wo = db.query(WorkOrder).filter(WorkOrder.dt_record_id == record_id).first()
            if wo:
                wo.email_trigger_status = "已发送"
                wo.email_sent = "是"
                db.commit()
                logger.info("Updated WorkOrder email status for record %s", record_id)
        except Exception as e:
            logger.warning("Failed to update WorkOrder email status for record %s: %s", record_id, e)

    # Clean up uploaded files on success
    if success and file_ids:
        for fid in file_ids.split(","):
            _upload_store.pop(fid.strip(), None)

    result = {"success": success, "message": message}
    if closure_result:
        result["closure"] = closure_result

    return result


# ── Email Pre-Analysis Endpoints ──────────────────────────────────────────


@router.get("/api/email-tool/pre-analysis")
async def get_pre_analysis_status(db: Session = Depends(get_db)):
    """Return pre-analysis results for all email-pending records.

    Returns a dict keyed by AITable record_id with analysis status and data.
    """
    from services.monitor_service import get_email_pending
    from services.email_pre_analysis import get_pre_analysis_for_records

    # Get current email-pending record IDs
    email_result = await get_email_pending(db)
    pending = email_result.get("pending", [])
    record_ids = [p.get("record_id", "") for p in pending if p.get("record_id")]

    # Fetch pre-analysis data for these records
    analyses = get_pre_analysis_for_records(db, record_ids)

    return {
        "pre_analysis": analyses,
        "total_pending": len(pending),
        "analyzed_count": len(analyses),
    }


@router.post("/api/email-tool/pre-analysis/run")
async def trigger_pre_analysis(
    auto_send: bool = Query(False, description="分析后自动发送邮件"),
    db: Session = Depends(get_db),
):
    """Manually trigger a pre-analysis cycle, optionally auto-send emails."""
    from services.email_pre_analysis import run_email_pre_analysis

    result = await run_email_pre_analysis(db, auto_send=auto_send)
    return {"status": "success", "result": result}


@router.post("/api/email-tool/send-direct")
async def send_email_direct(
    record_id: str = Form(...),
    extra_emails: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Send email using pre-analyzed data (no page navigation needed).

    This is the "直接发送" flow: uses cached AI data, re-downloads PDF for
    attachment, refreshes AITable fields for latest emails/sales.
    """
    from services.email_pre_analysis import send_email_from_pre_analysis

    email_list = None
    if extra_emails:
        email_list = [e.strip() for e in extra_emails.replace("，", ",").split(",") if e.strip() and "@" in e]

    result = await send_email_from_pre_analysis(db, record_id, extra_emails=email_list)

    # Record in history
    if result.get("status") == "success":
        from models.email_pre_analysis import EmailPreAnalysis
        analysis = db.query(EmailPreAnalysis).filter(
            EmailPreAnalysis.aitable_record_id == record_id,
        ).first()
        history = _load_history()
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "to": extra_emails or (analysis.emails if analysis else ""),
            "customer": analysis.customer_name if analysis else "",
            "product": analysis.product_name if analysis else "",
            "files": 1,
            "status": "成功",
        })
        _save_history(history[:50])

    return result
