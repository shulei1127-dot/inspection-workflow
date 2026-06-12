"""Email pre-analysis service: pre-analyze email-pending AITable records.

Separate sub-module that runs independently from the monitor poll.
Key design:
- Already-analyzed records are NOT re-analyzed (unique on aitable_record_id)
- Stores AI results only, NOT PDF content (re-download PDF at send time)
- At send time, lightweight refresh AITable fields (emails, sales), don't re-run AI
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from models.email_pre_analysis import EmailPreAnalysis
from services.aitable_fields import DISPATCH, extract_text, extract_select_name

logger = logging.getLogger(__name__)

# Product name short-name mapping for email subject
_PRODUCT_SHORT_NAMES = {
    "雷池": "雷池",
    "下一代Web应用防火墙": "雷池",
    "下一代 Web 应用防火墙": "雷池",
    "洞鉴": "洞鉴",
    "牧云": "牧云",
    "云工作负载保护平台": "牧云",
    "谛听": "谛听",
    "万象": "万象",
}

_PRODUCT_KEYWORDS = ["雷池", "洞鉴", "谛听", "牧云", "万象"]


def _short_product_name(name: str) -> str:
    """Return short product name for email subject.

    e.g. "下一代Web应用防火墙（雷池20系列）" → "雷池"
    """
    if not name:
        return ""
    # First check if any keyword is already in the name (e.g. "雷池20系列")
    for kw in _PRODUCT_KEYWORDS:
        if kw in name:
            return kw
    # Then check prefix mapping
    for full, short in _PRODUCT_SHORT_NAMES.items():
        if name.startswith(full) or full in name:
            return short
    return name

_HISTORY_FILE = Path(__file__).resolve().parent.parent / "email_tool" / "data" / "history.json"


def _record_send_history(customer: str, product: str, emails: list[str], files: int, success: bool) -> None:
    """Append a send record to the shared history file."""
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if _HISTORY_FILE.exists():
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        history.insert(0, {
            "time": datetime.now(timezone.utc).isoformat(),
            "customer": customer,
            "product": product,
            "to": ", ".join(emails),
            "status": "成功" if success else "失败",
            "files": files,
        })
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:50], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to record send history: %s", e)


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
        # AI may return summary as a list of strings — join with newlines
        if isinstance(summary, list):
            summary = "\n".join(str(s) for s in summary if s)
        return {
            "customer_name": info.get("customer_name", ""),
            "product_name": product,
            "quantity": info.get("quantity", ""),
            "emails": info.get("emails", []),
            "summary": summary,
            "summaries": [{"product": product, "summary": summary}],
        }

    # Multiple reports: merge with same logic as Streamlit
    # Filter out non-report PDFs (e.g. syslog guides) that have no summary and no date
    valid_infos = [
        info for info in ai_infos
        if info.get("summary") or info.get("inspection_date") or info.get("customer_name")
    ]
    if not valid_infos:
        valid_infos = ai_infos  # Fallback: use all if none match

    customer_name = ""
    product_names = []
    products_with_quantity = []
    all_emails = []
    summaries = []

    for info in valid_infos:
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

        s = info.get("summary", "")
        # AI may return summary as a list of strings — join with newlines
        if isinstance(s, list):
            s = "\n".join(str(item) for item in s if item)
        summaries.append({
            "product": prod or "产品",
            "summary": s,
        })

    summary = "\n\n".join(
        f"【{s['product']}】\n{s['summary']}" for s in summaries if s["summary"]
    )

    return {
        "customer_name": customer_name,
        "product_name": "、".join(product_names) if product_names else "",
        "quantity": "、".join(products_with_quantity) if products_with_quantity else "",
        "emails": list(dict.fromkeys(all_emails)),
        "summary": summary,
        "summaries": summaries,
    }


async def run_email_pre_analysis(db: Session, *, auto_send: bool = False) -> dict:
    """Pre-analyze email-pending AITable records that haven't been analyzed yet.

    If auto_send is True, automatically send email for successfully analyzed
    records that haven't been sent yet.

    Returns summary: {scanned, new, success, failed, skipped, sent, send_failed, send_skipped}
    """
    from services.monitor_service import get_email_pending

    # 1. Get current email-pending records from AITable
    email_result = await get_email_pending(db)
    pending_records = email_result.get("pending", [])
    scanned = len(pending_records)

    if scanned == 0:
        return {"scanned": 0, "new": 0, "success": 0, "failed": 0, "skipped": 0}

    new_count = 0
    success_count = 0
    failed_count = 0
    skipped_count = 0

    for item in pending_records:
        record_id = item.get("record_id", "")
        if not record_id:
            continue

        # 2. Check if already analyzed
        existing = db.query(EmailPreAnalysis).filter(
            EmailPreAnalysis.aitable_record_id == record_id,
        ).first()

        if existing and existing.analysis_status == "success":
            skipped_count += 1
            continue
        if existing and existing.analysis_status == "pending":
            # Still in-progress from a previous run; skip to avoid race
            skipped_count += 1
            continue

        # 3. Create or reset the analysis record
        if existing and existing.analysis_status == "failed":
            # Retry failed records
            existing.analysis_status = "pending"
            existing.error_message = None
            db.commit()
        else:
            analysis = EmailPreAnalysis(
                aitable_record_id=record_id,
                analysis_status="pending",
                customer_name=item.get("customer_name"),
                product_name=item.get("product_name"),
                emails=", ".join(item.get("email_addresses", [])),
            )
            db.add(analysis)
            db.commit()
            existing = analysis

        new_count += 1

        # 4. Download PDF and run AI extraction
        try:
            ai_result = await _analyze_single_record(db, record_id, existing)
            if ai_result.get("success"):
                success_count += 1
            else:
                failed_count += 1
        except Exception as e:
            logger.error("Pre-analysis failed for record %s: %s", record_id, e)
            existing.analysis_status = "failed"
            existing.error_message = str(e)[:500]
            existing.analyzed_at = datetime.now(timezone.utc)
            db.commit()
            failed_count += 1

    # Auto-send for successfully analyzed records
    sent = 0
    send_failed = 0
    send_skipped = 0

    if auto_send:
        eligible = db.query(EmailPreAnalysis).filter(
            EmailPreAnalysis.analysis_status == "success",
            EmailPreAnalysis.email_sent == False,
        ).all()

        for analysis in eligible:
            try:
                send_result = await send_email_from_pre_analysis(db, analysis.aitable_record_id)
                if send_result.get("status") == "success":
                    sent += 1
                    logger.info("Auto-send succeeded for record %s", analysis.aitable_record_id)
                else:
                    send_failed += 1
                    logger.warning("Auto-send failed for record %s: %s", analysis.aitable_record_id, send_result.get("message"))
            except Exception as e:
                send_failed += 1
                logger.error("Auto-send exception for record %s: %s", analysis.aitable_record_id, e)

    result = {
        "scanned": scanned,
        "new": new_count,
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "sent": sent,
        "send_failed": send_failed,
        "send_skipped": send_skipped,
    }
    logger.info("Email pre-analysis completed: %s", result)
    return result


async def _analyze_single_record(
    db: Session,
    record_id: str,
    analysis: EmailPreAnalysis,
) -> dict:
    """Download PDF from AITable, run AI extraction, persist results."""
    from services import dingtalk_client
    from services.email_sender import extract_info_with_ai
    from core.config import get_settings

    settings = get_settings()

    # Fetch the AITable record to get attachment URLs
    records = await dingtalk_client.query_records(
        limit=100,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
    )

    target = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target = record
            break

    if not target:
        analysis.analysis_status = "failed"
        analysis.error_message = f"AITable record {record_id} not found"
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": False, "error": "record not found"}

    cells = target.get("fields", {})
    report_attachments = cells.get(DISPATCH["巡检报告"])

    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        analysis.analysis_status = "failed"
        analysis.error_message = "No report attachment found"
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": False, "error": "no attachment"}

    # Download PDFs and extract text — only analyze PDFs that look like inspection reports
    import httpx
    import fitz  # PyMuPDF

    _INSPECTION_REPORT_KEYWORDS = ["巡检报告", "巡检", "Inspection", "inspection"]
    _PDF_EXTENSIONS = (".pdf", ".PDF")

    ai_infos = []
    download_errors = []
    has_inspection_pdf = False
    for att in report_attachments:
        if not isinstance(att, dict):
            continue
        url = att.get("url", "")
        filename = att.get("filename", "report.pdf")
        if not url:
            continue

        # Skip non-PDF files (they will still be sent as attachments at send time)
        if not filename.lower().endswith(".pdf"):
            logger.info("Skipping non-PDF attachment for AI analysis: %s", filename)
            continue

        # Only run AI on PDFs that look like inspection reports
        is_inspection_report = any(kw in filename for kw in _INSPECTION_REPORT_KEYWORDS)

        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                doc = fitz.open(stream=resp.content, filetype="pdf")
                pdf_text = ""
                for page in doc:
                    pdf_text += page.get_text()
                doc.close()

                if not pdf_text.strip():
                    download_errors.append(f"{filename}: PDF 文本为空")
                    continue

                if not is_inspection_report:
                    # Not an inspection report — skip AI analysis
                    logger.info("Skipping non-inspection PDF for AI analysis: %s", filename)
                    continue

                has_inspection_pdf = True
                info, ai_error = extract_info_with_ai(pdf_text)
                if info:
                    info["_filename"] = filename
                    ai_infos.append(info)
                if ai_error:
                    download_errors.append(f"{filename}: {ai_error}")
        except Exception as e:
            logger.warning("Failed to download attachment for analysis %s: %s", record_id, e)
            download_errors.append(f"{filename}: {e}")
            continue

    if not has_inspection_pdf:
        analysis.analysis_status = "failed"
        analysis.error_message = "未找到巡检报告PDF（文件名需包含'巡检报告'或'巡检'）" + (f" ({'; '.join(download_errors)})" if download_errors else "")
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": False, "error": "no inspection report PDF"}

    if not ai_infos:
        analysis.analysis_status = "failed"
        analysis.error_message = "所有 PDF 均无法提取有效信息" + (f" ({'; '.join(download_errors)})" if download_errors else "")
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": False, "error": "no valid PDF text"}

    # Merge multi-report results
    merged = _merge_multi_report_results(ai_infos)

    # Persist AI results
    analysis.analysis_status = "success"
    analysis.customer_name = merged.get("customer_name") or analysis.customer_name
    analysis.product_name = merged.get("product_name") or analysis.product_name
    analysis.inspection_date = ai_infos[0].get("inspection_date") if ai_infos else None
    analysis.quantity = merged.get("quantity") or analysis.quantity
    analysis.emails = ", ".join(merged.get("emails", [])) if merged.get("emails") else analysis.emails
    analysis.summary = merged.get("summary") or analysis.summary
    analysis.summaries = merged.get("summaries")
    analysis.ai_info = ai_infos if len(ai_infos) > 1 else (ai_infos[0] if ai_infos else None)
    analysis.analyzed_at = datetime.now(timezone.utc)
    db.commit()

    return {"success": True}


async def refresh_aitable_fields_for_send(
    db: Session,
    record_id: str,
) -> dict:
    """Lightweight refresh: fetch latest emails/sales from AITable, NOT PDF/AI.

    Returns the refreshed fields dict.
    """
    from services import dingtalk_client
    from core.config import get_settings

    settings = get_settings()
    analysis = db.query(EmailPreAnalysis).filter(
        EmailPreAnalysis.aitable_record_id == record_id,
    ).first()

    if not analysis:
        return {"error": "pre-analysis record not found"}

    # Fetch the AITable record for current field values
    records = await dingtalk_client.query_records(
        limit=100,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
    )

    target = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target = record
            break

    if not target:
        return {"error": "AITable record not found"}

    cells = target.get("fields", {})

    # Extract lightweight fields
    report_email = extract_text(cells.get(DISPATCH["报告发送邮箱"])) or ""
    sales_name = extract_text(cells.get(DISPATCH["销售"])) or ""
    customer_name = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    product_name = extract_text(cells.get(DISPATCH["产品名称"])) or ""

    # Parse email list
    email_list = []
    if report_email:
        for addr in report_email.replace("、", ",").replace("；", ",").replace("，", ",").split(","):
            addr = addr.strip()
            if addr and "@" in addr:
                email_list.append(addr)

    refreshed_fields = {
        "report_emails": email_list,
        "sales_name": sales_name,
        "customer_name": customer_name,
        "product_name": product_name,
        "email_sent_status": extract_select_name(cells.get(DISPATCH["邮件是否发送"])) or "",
    }

    # Persist refreshed fields
    analysis.aitable_fields = refreshed_fields
    analysis.refreshed_at = datetime.now(timezone.utc)
    db.commit()

    return refreshed_fields


async def preview_email_content(
    db: Session,
    record_id: str,
    extra_emails: list[str] | None = None,
) -> dict:
    """Preview email content without actually sending.

    Returns the composed subject, body, recipients, CC, and attachment filenames
    so the frontend can display a preview for manual confirmation.
    """
    analysis = db.query(EmailPreAnalysis).filter(
        EmailPreAnalysis.aitable_record_id == record_id,
    ).first()

    if not analysis:
        return {"status": "error", "message": "未找到预分析记录，请先运行预分析"}

    if analysis.analysis_status != "success":
        return {"status": "error", "message": f"预分析状态为 {analysis.analysis_status}，无法预览"}

    # Refresh AITable fields
    refreshed = await refresh_aitable_fields_for_send(db, record_id)
    if "error" in refreshed:
        return {"status": "error", "message": f"刷新 AITable 字段失败: {refreshed['error']}"}

    email_sent_status = refreshed.get("email_sent_status", "")
    if email_sent_status == "未上传":
        return {"status": "error", "message": "巡检报告标记为\"未上传\"，不允许发送邮件"}

    # Build recipient list
    email_list = []
    if extra_emails:
        email_list = extra_emails
    elif refreshed.get("report_emails"):
        email_list = refreshed["report_emails"]
    elif analysis.emails:
        email_list = [e.strip() for e in analysis.emails.split(",") if e.strip() and "@" in e]

    # Build CC list
    default_cc = ["jia.chen@chaitin.com", "kai.wu@chaitin.com", "lei.shu@chaitin.com"]
    cc_list = list(default_cc)
    sales_name = refreshed.get("sales_name", "")
    if sales_name:
        try:
            from services.email_sender import _get_name_pinyin
            sales_email = _get_name_pinyin(sales_name)
            if sales_email and sales_email not in cc_list:
                cc_list.append(sales_email)
        except Exception:
            pass

    # Compose email content (same logic as send_email_from_pre_analysis)
    customer_name = refreshed.get("customer_name") or analysis.customer_name or ""
    product_name = refreshed.get("product_name") or analysis.product_name or ""
    inspection_date = analysis.inspection_date or ""
    quantity = analysis.quantity or ""

    summaries = analysis.summaries
    if summaries and len(summaries) > 1:
        summary = "\n\n".join(
            f"【{s['product']}】\n{s['summary']}" for s in summaries if s.get("summary")
        )
    elif summaries and len(summaries) == 1:
        summary = summaries[0].get("summary", "") or analysis.summary or ""
    else:
        summary = analysis.summary or ""

    short_product = _short_product_name(product_name)
    date_display = (inspection_date or "近日").replace("-", ".")
    subject = f"【长亭科技巡检报告】{customer_name}{short_product}巡检报告-{date_display}"

    if quantity:
        if any(kw in quantity for kw in _PRODUCT_KEYWORDS):
            qty_display = quantity
        else:
            qty_display = f"{quantity}{short_product or product_name}"
    elif product_name:
        qty_display = short_product or product_name
    else:
        qty_display = "相关设备"

    body = (
        f"尊敬的客户，您好，\n"
        f"\n"
        f"非常感谢对长亭科技的信任！本司于 {inspection_date or '近日'} 对贵司的 {qty_display} 进行了一次全面的巡检，结果如下：\n"
        f"\n"
        f"{summary or '详见附件巡检报告。'}\n"
        f"\n"
        f"详细巡检报告见附件，请查收！\n"
        f"\n"
        f"后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～"
    )

    # Get attachment filenames from AITable
    from core.config import get_settings
    from services import dingtalk_client

    settings = get_settings()
    attachment_filenames = []
    try:
        records = await dingtalk_client.query_records(
            limit=100,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
            fetch_all=True,
        )
        for record in records:
            rid = record.get("recordId") or record.get("record_id", "")
            if rid == record_id:
                cells = record.get("fields", {})
                report_attachments = cells.get(DISPATCH["巡检报告"])
                if isinstance(report_attachments, list):
                    for att in report_attachments:
                        if isinstance(att, dict):
                            attachment_filenames.append(att.get("filename", "report.pdf"))
                break
    except Exception as e:
        logger.warning("Failed to fetch attachment filenames for preview: %s", e)

    return {
        "status": "success",
        "subject": subject,
        "body": body,
        "to_emails": email_list,
        "cc_emails": cc_list,
        "attachments": attachment_filenames,
        "customer_name": customer_name,
        "product_name": product_name,
        "inspection_date": inspection_date,
        "quantity": qty_display,
        "sales_name": sales_name,
    }


async def send_email_from_pre_analysis(
    db: Session,
    record_id: str,
    extra_emails: list[str] | None = None,
) -> dict:
    """Send email using pre-analyzed data. Re-download PDF, use cached AI.

    Flow:
    1. Get EmailPreAnalysis record from DB
    2. Refresh AITable fields for latest emails/sales
    3. Re-download PDF from AITable (for attachment)
    4. Compose email using pre-analyzed AI info + refreshed fields
    5. Send via SMTP (reuse existing send_email function)
    6. On success: write back AITable, auto-closure, update WorkOrder
    """
    from core.config import get_settings

    analysis = db.query(EmailPreAnalysis).filter(
        EmailPreAnalysis.aitable_record_id == record_id,
    ).first()

    if not analysis:
        return {"status": "error", "message": "未找到预分析记录，请先运行预分析"}

    if analysis.analysis_status != "success":
        return {"status": "error", "message": f"预分析状态为 {analysis.analysis_status}，无法直接发送"}

    # 1. Refresh AITable fields
    refreshed = await refresh_aitable_fields_for_send(db, record_id)
    if "error" in refreshed:
        return {"status": "error", "message": f"刷新 AITable 字段失败: {refreshed['error']}"}

    # Check if marked as "未上传" — do not send
    email_sent_status = refreshed.get("email_sent_status", "")
    if email_sent_status == "未上传":
        return {"status": "error", "message": "巡检报告标记为\"未上传\"，不允许发送邮件"}

    settings = get_settings()

    # 2. Build recipient list: prefer extra_emails > refreshed > pre-analyzed
    email_list = []
    if extra_emails:
        email_list = extra_emails
    elif refreshed.get("report_emails"):
        email_list = refreshed["report_emails"]
    elif analysis.emails:
        email_list = [e.strip() for e in analysis.emails.split(",") if e.strip() and "@" in e]

    if not email_list:
        return {"status": "error", "message": "客户邮箱为空，请先填写收件人邮箱"}

    # 3. Re-download PDF for attachment
    from services import dingtalk_client
    import httpx

    records = await dingtalk_client.query_records(
        limit=100,
        base_id=settings.dt_dispatch_base_id,
        table_id=settings.dt_dispatch_table_id,
        fetch_all=True,
    )

    target = None
    for record in records:
        rid = record.get("recordId") or record.get("record_id", "")
        if rid == record_id:
            target = record
            break

    if not target:
        return {"status": "error", "message": f"AITable 中未找到记录 {record_id}"}

    cells = target.get("fields", {})
    report_attachments = cells.get(DISPATCH["巡检报告"])

    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        return {"status": "error", "message": "巡检报告为空，无法发送邮件"}

    attachments = []
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
                attachments.append((filename, resp.content))
        except Exception as e:
            download_errors.append(f"{filename}: {e}")

    if not attachments and download_errors:
        return {"status": "error", "message": f"所有附件下载失败: {'; '.join(download_errors)}"}

    # 4. Compose email content using pre-analyzed AI info
    customer_name = refreshed.get("customer_name") or analysis.customer_name or ""
    product_name = refreshed.get("product_name") or analysis.product_name or ""
    inspection_date = analysis.inspection_date or ""
    quantity = analysis.quantity or ""

    # Build summary: multi-product → segmented by product; single → direct
    summaries = analysis.summaries
    if summaries and len(summaries) > 1:
        summary = "\n\n".join(
            f"【{s['product']}】\n{s['summary']}" for s in summaries if s.get("summary")
        )
    elif summaries and len(summaries) == 1:
        summary = summaries[0].get("summary", "") or analysis.summary or ""
    else:
        summary = analysis.summary or ""

    short_product = _short_product_name(product_name)
    date_display = (inspection_date or "近日").replace("-", ".")
    subject = f"【长亭科技巡检报告】{customer_name}{short_product}巡检报告-{date_display}"

    # Build quantity display: "2台雷池" / "雷池" (no quantity)
    # If quantity already contains product names (e.g. "1台洞鉴、2个探针谛听"), use it directly
    if quantity:
        if any(kw in quantity for kw in _PRODUCT_KEYWORDS):
            qty_display = quantity
        else:
            qty_display = f"{quantity}{short_product or product_name}"
    elif product_name:
        qty_display = short_product or product_name
    else:
        qty_display = "相关设备"

    body = (
        f"尊敬的客户，您好，\n"
        f"\n"
        f"非常感谢对长亭科技的信任！本司于 {inspection_date or '近日'} 对贵司的 {qty_display} 进行了一次全面的巡检，结果如下：\n"
        f"\n"
        f"{summary or '详见附件巡检报告。'}\n"
        f"\n"
        f"详细巡检报告见附件，请查收！\n"
        f"\n"
        f"后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～"
    )

    # 5. Send email
    from services.email_sender import send_email as _send_email

    # Build CC: default CC + sales email
    default_cc = ["jia.chen@chaitin.com", "kai.wu@chaitin.com", "lei.shu@chaitin.com"]
    cc_list = list(default_cc)
    sales_name = refreshed.get("sales_name", "")
    if sales_name:
        try:
            from services.email_sender import _get_name_pinyin
            sales_email = _get_name_pinyin(sales_name)
            if sales_email and sales_email not in cc_list:
                cc_list.append(sales_email)
        except Exception:
            pass

    success, message = _send_email(
        to_emails=email_list,
        subject=subject,
        body=body,
        attachments=attachments if attachments else None,
        cc_emails=",".join(cc_list),
    )

    if not success:
        return {"status": "failed", "message": message}

    # Record in send history
    _record_send_history(customer_name, product_name, email_list, len(attachments), True)

    # 6. Post-send: write back AITable, auto-closure, update WorkOrder
    from services.monitor_service import _write_back_email_sent, _invalidate_email_cache, _invalidate_aitable_cache

    try:
        await _write_back_email_sent(
            record_id=record_id,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
        )
        _invalidate_email_cache()
        _invalidate_aitable_cache(settings.dt_dispatch_base_id, settings.dt_dispatch_table_id)
    except Exception as e:
        logger.warning("Failed to write back email_sent to AITable: %s", e)

    # Auto-close the corresponding work order
    closure_result = None
    try:
        from services.pts_closure_service import close_work_order_after_email
        closure_result = await close_work_order_after_email(db, record_id)
    except Exception as e:
        logger.warning("Auto-closure failed for record %s: %s", record_id, e)
        closure_result = {"success": False, "message": f"闭环异常: {e}"}

    # Update WorkOrder email status
    try:
        from models.work_order import WorkOrder
        wo = db.query(WorkOrder).filter(WorkOrder.dt_record_id == record_id).first()
        if wo:
            wo.email_trigger_status = "已发送"
            wo.email_sent = "是"
    except Exception as e:
        logger.warning("Failed to update WorkOrder email status: %s", e)

    # Mark pre-analysis as email sent
    try:
        analysis.email_sent = True
        db.commit()
    except Exception as e:
        logger.warning("Failed to mark EmailPreAnalysis email_sent: %s", e)

    result = {"status": "success", "message": message}
    if closure_result:
        result["closure"] = closure_result
    if download_errors:
        result["download_warnings"] = download_errors

    return result


def get_pre_analysis_for_records(db: Session, record_ids: list[str]) -> dict[str, dict]:
    """Get pre-analysis status for a list of AITable record IDs.

    Returns {record_id: {analysis_status, customer_name, ...}} dict.
    """
    if not record_ids:
        return {}

    analyses = db.query(EmailPreAnalysis).filter(
        EmailPreAnalysis.aitable_record_id.in_(record_ids),
    ).all()

    result = {}
    for a in analyses:
        result[a.aitable_record_id] = {
            "analysis_status": a.analysis_status,
            "customer_name": a.customer_name,
            "product_name": a.product_name,
            "inspection_date": a.inspection_date,
            "quantity": a.quantity,
            "emails": a.emails,
            "summary": a.summary,
            "summaries": a.summaries,
            "error_message": a.error_message,
            "email_sent": a.email_sent,
            "analyzed_at": a.analyzed_at.isoformat() if a.analyzed_at else None,
        }

    return result
