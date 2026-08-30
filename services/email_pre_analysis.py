"""Email pre-analysis service: pre-analyze email-pending AITable records.

Separate sub-module that runs independently from the monitor poll.
Key design:
- Already-analyzed records are NOT re-analyzed (unique on aitable_record_id)
- Stores AI results only, NOT PDF content (re-download PDF at send time)
- At send time, lightweight refresh AITable fields (emails, sales), don't re-run AI
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models.email_pre_analysis import EmailPreAnalysis
from services.aitable_fields import DISPATCH, extract_text

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

# 平台级运行状态巡检类产品（报告无"X台"设备数量，只有探针在线/离线数）
_PLATFORM_PRODUCT_KEYWORDS = ("牧云", "cloudwalker", "云工作负载")


def _is_platform_product(short_name: str) -> bool:
    """Whether a short product name belongs to the platform run-state class (牧云/CloudWalker)."""
    if not short_name:
        return False
    lowered = short_name.lower()
    return any(kw in lowered for kw in _PLATFORM_PRODUCT_KEYWORDS)

# 产品显示名兜底（优先取 AITable 产品名称字段中匹配的段，例如"云工作负载保护平台（牧云）"）
_PRODUCT_FULL_NAMES = {
    "牧云": "云工作负载保护平台（牧云）",
    "CloudWalker": "云工作负载保护平台（牧云）",
    "雷池": "下一代Web应用防火墙（雷池20系列）",
    "洞鉴": "风险评估系统（洞鉴）",
    "谛听": "主动威胁欺骗防御系统（谛听）",
    "万象": "安全分析与运营管理平台（万象）",
    "全悉": "流量威胁检测响应系统（全悉）",
}


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


def _resolve_full_product_name(short_kw: str, aitable_field: str = "") -> str:
    """Resolve full display product name.

    Prefers the segment of the AITable 产品名称 multi-product field that contains
    the short keyword, e.g. short "牧云" + "云工作负载保护平台（牧云）、主动威胁欺骗防御系统（谛听）"
    → "云工作负载保护平台（牧云）". Falls back to a static full-name map.
    """
    if aitable_field and short_kw:
        for seg in re.split(r"[、，,;/+]+", aitable_field):
            seg = seg.strip()
            if seg and short_kw in seg:
                return seg
    return _PRODUCT_FULL_NAMES.get(short_kw, "")


def _merge_multi_report_results(ai_infos: list[dict]) -> dict:
    """Merge AI extraction results from multiple PDF reports.

    Returns merged dict with: customer_name, product_name, quantity, emails,
    summary, summaries.

    When the same product appears in multiple PDFs, quantities are aggregated
    and duplicated summaries are removed.
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
    product_order: list[str] = []
    product_quantities: dict[str, list[str]] = {}  # product → [quantity_str, ...]
    all_emails: list[str] = []
    product_summaries: dict[str, list[str]] = {}   # product → [summary, ...]

    for info in valid_infos:
        if not customer_name and info.get("customer_name"):
            customer_name = info["customer_name"]

        prod = info.get("product_name", "")
        # Normalize product name — take the short keyword form
        for kw in _PRODUCT_KEYWORDS:
            if kw in prod:
                prod = kw
                break

        if not prod:
            prod = "产品"

        if prod not in product_order:
            product_order.append(prod)

        qty = info.get("quantity", "")
        if qty:
            product_quantities.setdefault(prod, []).append(qty)

        if info.get("emails"):
            all_emails.extend(info["emails"])

        s = info.get("summary", "")
        # AI may return summary as a list of strings — join with newlines
        if isinstance(s, list):
            s = "\n".join(str(item) for item in s if item)
        if s:
            product_summaries.setdefault(prod, []).append(s)

    # Build deduplicated summaries and quantities per product
    summaries: list[dict] = []
    products_with_quantity: list[str] = []

    for prod in product_order:
        qty_list = product_quantities.get(prod, [])
        sum_list = product_summaries.get(prod, [])

        if sum_list:
            # Pair quantities with summaries; when summaries are duplicates,
            # the corresponding quantities refer to the SAME devices and
            # should NOT be aggregated.
            unique = _deduplicate_summaries(sum_list)
            unique_count = len(unique)
            original_count = len(sum_list)

            if unique_count == 1 or unique_count < original_count:
                # Duplicates detected — use single quantity
                merged_qty = qty_list[0] if qty_list else ""
            else:
                merged_qty = _merge_quantities(qty_list) if qty_list else ""

            if merged_qty:
                products_with_quantity.append(f"{merged_qty}{prod}")

            merged_summary = _format_summary("\n\n".join(unique))
            summaries.append({"product": prod, "summary": merged_summary})
        elif qty_list:
            merged_qty = _merge_quantities(qty_list)
            products_with_quantity.append(f"{merged_qty}{prod}")
            summaries.append({"product": prod, "summary": ""})
        else:
            products_with_quantity.append(prod)
            summaries.append({"product": prod, "summary": ""})

    summary = "\n\n".join(
        f"【{s['product']}】\n{s['summary']}" for s in summaries if s["summary"]
    )

    return {
        "customer_name": customer_name,
        "product_name": "、".join(product_order) if product_order else "",
        "quantity": "、".join(products_with_quantity) if products_with_quantity else "",
        "emails": list(dict.fromkeys(all_emails)),
        "summary": summary,
        "summaries": summaries,
    }


def _merge_quantities(quantities: list[str]) -> str:
    """Aggregate quantity strings, e.g. ['四台','四台'] → '八台'.

    Handles Chinese numerals (一～十) and Arabic digits, preserves unit suffix.
    """
    import re

    if not quantities:
        return ""

    # Map Chinese numerals to numbers
    CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
              "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20}

    NUM_CN = {v: k for k, v in CN_NUM.items()}

    total = 0
    unit = ""
    for q in quantities:
        q = q.strip()
        # Try Chinese number + unit, e.g. "四台", "八台"
        m = re.match(r'^([一二三四五六七八九十]+)(\S+)$', q)
        if m:
            total += CN_NUM.get(m.group(1), 1)
            if not unit:
                unit = m.group(2)
            continue
        # Try Arabic digit + unit, e.g. "4台", "8台"
        m = re.match(r'^(\d+)(\S+)$', q)
        if m:
            total += int(m.group(1))
            if not unit:
                unit = m.group(2)
            continue
        # Can't parse, return first as-is
        return quantities[0]

    if total == 0:
        return quantities[0]

    # Convert back to original numeral style if input was Chinese
    if all(re.match(r'^[一二三四五六七八九十]', q) for q in quantities):
        if total in NUM_CN:
            return f"{NUM_CN[total]}{unit}"
    return f"{total}{unit}"


def _deduplicate_summaries(summaries: list[str]) -> list[str]:
    """Deduplicate near-identical summary texts.

    Keeps only unique summaries; removes those that are substantially
    similar to another (e.g. same content with minor whitespace/formatting
    differences).
    """
    if not summaries:
        return []
    if len(summaries) == 1:
        return summaries

    import re
    from difflib import SequenceMatcher

    def _normalize(s: str) -> str:
        """Collapse whitespace for robust comparison."""
        return re.sub(r'\s+', ' ', s).strip()

    result: list[str] = []
    normalized_seen: list[str] = []

    for s in summaries:
        s = s.strip()
        if not s:
            continue
        norm = _normalize(s)

        is_dup = False
        for seen_norm in normalized_seen:
            # 1. Exact match after whitespace normalization
            if norm == seen_norm:
                is_dup = True
                break
            # 2. Substring containment (one contains the other, with sufficient length)
            shorter, longer = (norm, seen_norm) if len(norm) <= len(seen_norm) else (seen_norm, norm)
            if len(shorter) > 20 and shorter in longer:
                is_dup = True
                break
            # 3. Fuzzy similarity: high sequence ratio → near-identical content
            # Threshold 0.95 catches whitespace-only diffs but not
            # genuinely different summaries (different devices/regions)
            if SequenceMatcher(None, norm, seen_norm).ratio() >= 0.95:
                is_dup = True
                break

        if not is_dup:
            result.append(s)
            normalized_seen.append(norm)

    return result if result else [summaries[0]]


def _format_summary(summary: str) -> str:
    """Post-process summary text: add line breaks before numbered items.

    AI-generated summaries often produce one long paragraph; this inserts
    newlines before enumeration markers so the email body is readable.

    e.g. "…WAF：1、站点防护…；2、规则…" → "…WAF：\n1、站点防护…；\n2、规则…"
    """
    import re
    if not summary:
        return summary

    # Add newline before Chinese enumeration: 1、 2、 3、 etc.
    # Uses negative lookbehind to avoid matching version digits (e.g. 5.10.15)
    summary = re.sub(r'(?<!\d)(\d{1,2})、\s*', r'\n\1、', summary)

    # Add newline before Arabic enumeration: 1. 2. etc.
    # Avoid matching version numbers (preceded by digit or dot)
    summary = re.sub(r'(?<![\d.])(\d{1,2})\.\s+', r'\n\1. ', summary)

    # Add newline before section markers like "灾备区域WAF："
    # Pattern: Chinese text ending with ： that follows a clause separator
    summary = re.sub(r'(?<=[。；])\s*([一-鿿]+WAF[：:])', r'\n\1', summary)
    summary = re.sub(r'(?<=[。；])\s*([一-鿿]+区域[：:])', r'\n\1', summary)

    # Clean up leading/trailing whitespace
    summary = summary.strip()

    return summary


def _consolidate_email_data(
    summaries: list[dict] | None,
    quantity: str = "",
) -> tuple[list[dict], str]:
    """Consolidate summaries and quantity at runtime to handle stale data.

    Groups duplicate product entries, deduplicates summaries within each
    product, and re-aggregates quantities (e.g. "四台雷池、四台雷池" → "八台雷池").

    Returns (consolidated_summaries, consolidated_quantity).
    """
    if not summaries:
        return summaries or [], quantity

    # 1. Group summaries by normalized product name
    product_order: list[str] = []
    product_summaries: dict[str, list[str]] = {}
    product_qtys: dict[str, list[str]] = {}

    for s in summaries:
        prod = s.get("product", "")
        # Normalize product name to short keyword form
        for kw in _PRODUCT_KEYWORDS:
            if kw in prod:
                prod = kw
                break
        if not prod:
            prod = "产品"

        if prod not in product_order:
            product_order.append(prod)

        if s.get("summary"):
            product_summaries.setdefault(prod, []).append(s["summary"])

    # 2. Re-aggregate quantity if it contains duplicate products
    if quantity and "、" in quantity:
        for part in quantity.split("、"):
            part = part.strip()
            if not part:
                continue
            # Find the product keyword in this part
            prod = ""
            for kw in _PRODUCT_KEYWORDS:
                if kw in part:
                    prod = kw
                    break
            if not prod:
                continue
            # Extract quantity portion (everything before the product name)
            qty_part = part.replace(prod, "").strip()
            if qty_part:
                product_qtys.setdefault(prod, []).append(qty_part)

    # 3. Build consolidated summaries
    consolidated: list[dict] = []
    products_with_quantity: list[str] = []

    for prod in product_order:
        sum_list = product_summaries.get(prod, [])
        qtys = product_qtys.get(prod, [])

        if sum_list:
            unique = _deduplicate_summaries(sum_list)
            merged_text = _format_summary("\n\n".join(unique))
            consolidated.append({"product": prod, "summary": merged_text})

            # Only aggregate quantities for genuinely different entries;
            # duplicate summaries → duplicate quantities of same devices
            if qtys:
                if len(unique) == 1 or len(unique) < len(sum_list):
                    merged_qty = qtys[0]  # duplicates → single quantity
                else:
                    merged_qty = _merge_quantities(qtys)
                products_with_quantity.append(f"{merged_qty}{prod}")
        else:
            consolidated.append({"product": prod, "summary": ""})
            if qtys:
                merged_qty = _merge_quantities(qtys)
                products_with_quantity.append(f"{merged_qty}{prod}")

    new_quantity = "、".join(products_with_quantity) if products_with_quantity else quantity

    return consolidated, new_quantity


async def run_email_pre_analysis(db: Session) -> dict:
    """Pre-analyze email-pending AITable records that haven't been analyzed yet.

    Returns summary: {scanned, new, success, failed, skipped}
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

    result = {
        "scanned": scanned,
        "new": new_count,
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
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

    cells = target.get("cells", {})
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

    # First pass: download all PDFs and classify them
    # (non-PDF files like Word docs are skipped for AI analysis but will be sent as email attachments)
    pdf_texts: list[tuple[str, str, bool]] = []  # (filename, text, is_inspection_report)
    download_errors = []
    for att in report_attachments:
        if not isinstance(att, dict):
            continue
        url = att.get("url", "")
        filename = att.get("filename", "report.pdf")
        if not url:
            continue

        # Skip non-PDF files for AI analysis.
        # Word docs (.doc/.docx) are NOT sent to customers (PDF report is sufficient).
        # Other non-PDF files (contracts, authorization letters, etc.) ARE sent as email attachments.
        if not filename.lower().endswith(".pdf"):
            logger.info("Skipping non-PDF attachment for AI analysis: %s", filename)
            continue

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

                pdf_texts.append((filename, pdf_text, is_inspection_report))
        except Exception as e:
            logger.warning("Failed to download attachment for analysis %s: %s", record_id, e)
            download_errors.append(f"{filename}: {e}")
            continue

    # Second pass: decide which PDFs to run AI on
    # Strategy: prefer PDFs whose filename matches inspection report keywords;
    # if none match, fall back to analyzing ALL PDFs (degradation to avoid false negatives)
    inspection_pdfs = [(fn, txt) for fn, txt, is_ir in pdf_texts if is_ir]
    other_pdfs = [(fn, txt) for fn, txt, is_ir in pdf_texts if not is_ir]

    if inspection_pdfs:
        ai_candidates = inspection_pdfs
        for fn, _ in other_pdfs:
            logger.info("Skipping non-inspection PDF for AI analysis: %s (inspection PDFs found)", fn)
    elif other_pdfs:
        # No PDF matched inspection keywords — fall back to analyzing all PDFs
        ai_candidates = other_pdfs
        logger.info(
            "No PDF filename matched inspection keywords, falling back to analyzing all %d PDF(s)",
            len(other_pdfs),
        )
    else:
        ai_candidates = []

    if not ai_candidates:
        analysis.analysis_status = "failed"
        analysis.error_message = "未找到巡检报告PDF（文件名需包含'巡检报告'或'巡检'）" + (f" ({'; '.join(download_errors)})" if download_errors else "")
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.commit()
        return {"success": False, "error": "no inspection report PDF"}

    # Run AI extraction on selected PDFs
    ai_infos = []
    for filename, pdf_text in ai_candidates:
        info, ai_error = extract_info_with_ai(pdf_text)
        if info:
            info["_filename"] = filename
            ai_infos.append(info)
        if ai_error:
            download_errors.append(f"{filename}: {ai_error}")

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
    # 平台级巡检（牧云/CloudWalker）：数量=被巡检的管理端数量=巡检报告份数（如"1台"）
    platform_infos = [
        i for i in ai_infos
        if _is_platform_product(_short_product_name(i.get("product_name", "")))
    ]
    if platform_infos and len(platform_infos) == len(ai_infos):
        analysis.quantity = f"{len(platform_infos)}台"
    else:
        analysis.quantity = merged.get("quantity") or analysis.quantity
    ai_emails = merged.get("emails", [])
    if ai_emails:
        analysis.emails = ", ".join(ai_emails)
    elif not analysis.emails:
        # AI didn't find emails in PDF, fallback to AITable "报告发送邮箱" field
        from services.monitor_service import get_email_pending
        try:
            email_result = await get_email_pending(db)
            for item in email_result.get("pending", []):
                if item.get("record_id") == record_id:
                    ait_emails = item.get("email_addresses", [])
                    if ait_emails:
                        analysis.emails = ", ".join(ait_emails)
                    break
        except Exception:
            pass
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

    cells = target.get("cells", {})

    # Extract lightweight fields
    report_email = extract_text(cells.get(DISPATCH["报告发送邮箱"])) or ""
    sales_name = extract_text(cells.get(DISPATCH["销售"])) or ""
    customer_name = extract_text(cells.get(DISPATCH["客户名称"])) or ""
    product_name = extract_text(cells.get(DISPATCH["产品名称"])) or ""

    # Parse email list
    email_list = []
    if report_email:
        for addr in report_email.replace("\n", ",").replace("\r", "").replace("、", ",").replace("；", ",").replace("，", ",").split(","):
            addr = addr.strip()
            if addr and "@" in addr:
                email_list.append(addr)

    from services.aitable_fields import extract_select_name

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


def _compose_email_content(
    customer_name: str,
    product_name: str,
    aitable_product_name: str,
    inspection_date: str,
    quantity: str,
    summaries: list[dict] | None,
    analysis_summary: str,
    ai_info: dict | list | None = None,
) -> dict:
    """Shared email composition for send & preview.

    - Subject uses the short product name (牧云/谛听/雷池...).
    - Body/preview display the full product name (e.g. 云工作负载保护平台（牧云）)
      resolved from the AITable 产品名称 field, falling back to a static map.
    - Platform run-state reports (牧云/CloudWalker): the inspected quantity is the
      number of management ends, i.e. the number of inspection reports (e.g. "1台"),
      derived from the consolidated summaries for the preview 数量 field.
    """
    consolidated, quantity = _consolidate_email_data(summaries, quantity)

    if consolidated and len(consolidated) > 1:
        summary = "\n\n".join(
            f"【{s['product']}】\n{s['summary']}" for s in consolidated if s.get("summary")
        )
    elif consolidated and len(consolidated) == 1:
        summary = consolidated[0].get("summary", "") or analysis_summary or ""
    else:
        summary = analysis_summary or ""

    short_product = _short_product_name(product_name)
    full_product = _resolve_full_product_name(short_product, aitable_product_name)
    date_display = (inspection_date or "近日").replace("-", ".")
    subject = f"【长亭科技巡检报告】{customer_name}{short_product}巡检报告-{date_display}"

    # 平台级巡检（牧云/CloudWalker）：数量=被巡检的管理端数量=巡检报告份数（如"1台"）
    derived_quantity = ""
    if short_product in _PLATFORM_PRODUCT_KEYWORDS:
        infos = ai_info if isinstance(ai_info, list) else ([ai_info] if ai_info else [])
        if infos:
            platform_count = sum(
                1 for i in infos
                if _is_platform_product(_short_product_name(i.get("product_name", "")))
            )
            if platform_count and platform_count == len(infos):
                quantity = f"{platform_count}台"
                derived_quantity = quantity

    # Build quantity display for the body: "1台谛听" / "1台云工作负载保护平台（牧云）"
    if quantity:
        if any(kw in quantity for kw in _PRODUCT_KEYWORDS):
            qty_display = quantity
        elif short_product in _PLATFORM_PRODUCT_KEYWORDS and full_product:
            qty_display = f"{quantity}{full_product}"
        else:
            qty_display = f"{quantity}{short_product or product_name}"
    elif full_product:
        qty_display = full_product
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

    return {
        "subject": subject,
        "body": body,
        "display_product_name": full_product or product_name or short_product or "产品",
        "display_quantity": derived_quantity or qty_display,
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
        email_list = [e.replace("\n", "").replace("\r", "").strip() for e in analysis.emails.replace("、", ",").replace("；", ",").replace("，", ",").split(",") if e.strip() and "@" in e]

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

    cells = target.get("cells", {})
    report_attachments = cells.get(DISPATCH["巡检报告"])

    if not isinstance(report_attachments, list) or len(report_attachments) == 0:
        return {"status": "error", "message": "巡检报告为空，无法发送邮件"}

    attachments = []
    download_errors = []
    _WORD_EXTENSIONS = (".doc", ".docx")
    for att in report_attachments:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename", "report.pdf")
        url = att.get("url", "")
        if not url:
            download_errors.append(f"{filename}: 无下载链接")
            continue
        # Skip Word docs (.doc/.docx) — PDF report is sufficient for customers
        if filename.lower().endswith(_WORD_EXTENSIONS):
            logger.info("Skipping Word doc for email attachment: %s", filename)
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
    # 产品名优先取预分析（AI 从实际报告提取），避免 AITable 多产品字段（如"牧云、谛听"）
    # 导致邮件主题/正文使用错误的产品短名
    product_name = analysis.product_name or refreshed.get("product_name") or ""
    inspection_date = analysis.inspection_date or ""
    quantity = analysis.quantity or ""

    composed = _compose_email_content(
        customer_name=customer_name,
        product_name=product_name,
        aitable_product_name=refreshed.get("product_name") or "",
        inspection_date=inspection_date,
        quantity=quantity,
        summaries=analysis.summaries,
        analysis_summary=analysis.summary or "",
        ai_info=analysis.ai_info,
    )
    subject = composed["subject"]
    body = composed["body"]

    # 5. Send email
    from services.email_sender import send_email as _send_email

    # Build CC: default CC + sales email
    default_cc = ["kai.wu@chaitin.com", "lei.shu@chaitin.com"]
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
        from services.pts_closure_service import handle_email_success
        closure_result = await handle_email_success(db, record_id, legacy_closure=True)
    except Exception as e:
        logger.warning("Auto-closure failed for record %s: %s", record_id, e)
        closure_result = {"success": False, "message": f"闭环异常: {e}"}

    # Update WorkOrder email status
    try:
        from models.work_order import WorkOrder
        from models.work_order_sync import WorkOrderSync
        wo = db.query(WorkOrder).join(
            WorkOrderSync,
            WorkOrderSync.work_order_id == WorkOrder.id,
        ).filter(WorkOrderSync.aitable_record_id == record_id).first()
        if wo is None:
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
        email_list = [e.replace("\n", "").replace("\r", "").strip() for e in analysis.emails.replace("、", ",").replace("；", ",").replace("，", ",").split(",") if e.strip() and "@" in e]

    # Build CC list
    default_cc = ["kai.wu@chaitin.com", "lei.shu@chaitin.com"]
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
    # 产品名优先取预分析（AI 从实际报告提取），避免 AITable 多产品字段（如"牧云、谛听"）选错短名
    product_name = analysis.product_name or refreshed.get("product_name") or ""
    inspection_date = analysis.inspection_date or ""
    quantity = analysis.quantity or ""

    composed = _compose_email_content(
        customer_name=customer_name,
        product_name=product_name,
        aitable_product_name=refreshed.get("product_name") or "",
        inspection_date=inspection_date,
        quantity=quantity,
        summaries=analysis.summaries,
        analysis_summary=analysis.summary or "",
        ai_info=analysis.ai_info,
    )
    subject = composed["subject"]
    body = composed["body"]

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
                cells = record.get("cells", {})
                report_attachments = cells.get(DISPATCH["巡检报告"])
                if isinstance(report_attachments, list):
                    for att in report_attachments:
                        if isinstance(att, dict):
                            fn = att.get("filename", "report.pdf")
                            # Skip Word docs in preview (same filter as send)
                            if fn.lower().endswith((".doc", ".docx")):
                                continue
                            attachment_filenames.append(fn)
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
        "product_name": composed["display_product_name"],
        "inspection_date": inspection_date,
        "quantity": composed["display_quantity"],
        "sales_name": sales_name,
    }
