"""Read-only inspection report scanner and hybrid audit engine.

The module only reads the dispatch AITable and report attachments. It never
writes to AITable, sends mail, or advances a PTS work order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from xml.etree import ElementTree

import httpx
from sqlalchemy.orm import Session

from core.config import get_settings
from models.report_audit import ReportAudit
from services import dingtalk_client
from services.aitable_fields import DISPATCH, extract_pts_order_id_from_link, extract_select_name, extract_text

logger = logging.getLogger(__name__)

RULE_VERSION = "2026.09.1"
SUPPORTED_EXTENSIONS = (".pdf", ".docx")
SEVERITY_WEIGHT = {"blocker": 25, "error": 12, "warning": 5, "info": 0}
_SCAN_LOCK = asyncio.Lock()

PRODUCT_ALIASES = {
    "雷池": ("雷池", "safeline", "waf", "web应用防火墙"),
    "洞鉴": ("洞鉴", "风险评估"),
    "谛听": ("谛听", "d-sensor", "dsensor", "伪装欺骗", "主动威胁欺骗"),
    "牧云": ("牧云", "cloudwalker", "云工作负载", "cwpp"),
    "万象": ("万象", "安全分析与运营管理平台"),
    "墨攻": ("墨攻",),
    "全悉": ("全悉",),
}


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    evidence: str,
    suggestion: str,
    *,
    source: str = "rule",
    page: int | None = None,
) -> dict:
    item = {
        "rule_id": rule_id,
        "severity": severity,
        "title": title,
        "evidence": evidence[:1000],
        "suggestion": suggestion[:1000],
        "source": source,
    }
    if page:
        item["page"] = page
    return item


def _normalize(value: str) -> str:
    return re.sub(r"[\s\-—_·•（）()【】\[\]，,。.:：;；/\\]", "", value or "").lower()


def _product_family(value: str) -> str | None:
    normalized = _normalize(value)
    for family, aliases in PRODUCT_ALIASES.items():
        if any(_normalize(alias) in normalized for alias in aliases):
            return family
    return None


def attachment_fingerprint(attachments: list[dict]) -> str:
    """Build a stable fingerprint without expiring signed URLs."""
    safe = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        safe.append(
            {
                "resource_id": attachment.get("resourceId") or attachment.get("fileId") or attachment.get("id") or "",
                "filename": attachment.get("filename") or attachment.get("name") or "",
                "size": attachment.get("size") or attachment.get("fileSize") or 0,
            }
        )
    payload = json.dumps(
        sorted(safe, key=lambda value: (str(value["resource_id"]), value["filename"])),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_attachment_rules(attachments: list[dict]) -> list[dict]:
    names = [str(item.get("filename") or item.get("name") or "") for item in attachments if isinstance(item, dict)]
    has_pdf = any(name.lower().endswith(".pdf") for name in names)
    has_docx = any(name.lower().endswith(".docx") for name in names)
    if has_pdf and has_docx:
        return []
    missing = "、".join(label for condition, label in ((has_docx, "Word（DOCX）"), (has_pdf, "PDF")) if not condition)
    return [
        _finding(
            "COMMON-010",
            "warning",
            "Word 与 PDF 未同时上传",
            f"当前附件：{'、'.join(names) or '无'}；缺少 {missing} 版本。",
            "按自检清单同时上传内容一致的 Word（DOCX）和 PDF 两份报告。",
        )
    ]


def run_hard_rules(customer: str, product: str, filename: str, text: str, page_count: int | None = None) -> list[dict]:
    """Run deterministic checks that can be proven from extracted text."""
    findings: list[dict] = []
    normalized_text = _normalize(text)
    normalized_filename = _normalize(filename)
    normalized_customer = _normalize(customer)

    if len(normalized_customer) >= 4 and normalized_customer not in normalized_text:
        findings.append(
            _finding(
                "COMMON-001",
                "blocker",
                "报告客户名称与派单记录不一致",
                f"派单客户为“{customer}”，报告正文未检出该客户名称。",
                "核对附件是否上传到了正确客户记录，并统一封面、页眉和正文客户名称。",
            )
        )
    elif len(normalized_customer) >= 4 and normalized_customer not in normalized_filename:
        findings.append(
            _finding(
                "COMMON-002",
                "warning",
                "文件名未包含当前客户名称",
                f"文件名“{filename}”未包含派单客户“{customer}”。",
                "建议按“客户全称+产品名称+巡检报告+日期”规范命名。",
            )
        )

    expected_family = _product_family(product)
    actual_family = _product_family(filename + "\n" + text[:8000])
    if expected_family and actual_family and expected_family != actual_family:
        findings.append(
            _finding(
                "COMMON-003",
                "blocker",
                "报告产品与派单产品不一致",
                f"派单产品识别为“{expected_family}”，报告识别为“{actual_family}”。",
                "检查是否误传了其他产品的报告，并重新上传正确附件。",
            )
        )
    elif expected_family and not actual_family:
        findings.append(
            _finding(
                "COMMON-004",
                "warning",
                "报告中未明确识别出派单产品",
                f"派单产品为“{product}”，但文件名和正文中未可靠识别产品名称。",
                "在封面或巡检概述中明确写明产品名称。",
            )
        )

    toc_errors = (
        "toc will populate after updating fields",
        "error! no table of contents entries found",
        "错误!未找到目录项",
        "错误!未定义书签",
    )
    compact_lowered = re.sub(r"\s+", "", text.lower())
    matched_toc = next((value for value in toc_errors if re.sub(r"\s+", "", value) in compact_lowered), None)
    if matched_toc:
        findings.append(
            _finding(
                "COMMON-005",
                "blocker",
                "目录未正确生成",
                f"报告中检测到目录错误占位文字：{matched_toc}",
                "在 Word 中更新全部域并确认目录正常显示后，重新导出 PDF。",
            )
        )

    if expected_family != "牧云" and "设备巡检信息汇总" not in re.sub(r"\s+", "", text):
        findings.append(
            _finding(
                "COMMON-006",
                "blocker",
                "缺少设备巡检信息汇总",
                "报告正文未检测到“设备巡检信息汇总”章节；牧云报告除外。",
                "在报告末尾添加设备巡检信息汇总表，并确保设备数量与正文一致。",
            )
        )

    placeholders = sorted(
        set(
            re.findall(
                r"(?:待补充|待填写|请填写|客户名称\s*X{2,}|X{4,}|<[^>]{1,30}>|【(?:填写|待填)[^】]*】)",
                text,
                re.IGNORECASE,
            )
        )
    )
    if placeholders:
        findings.append(
            _finding(
                "COMMON-007",
                "warning",
                "报告中存在模板占位内容",
                "、".join(placeholders[:10]),
                "替换或删除所有模板占位文字后重新提交。",
            )
        )

    if len(text.strip()) < 800:
        findings.append(
            _finding(
                "COMMON-008",
                "error",
                "报告可提取内容过少",
                f"仅提取到 {len(text.strip())} 个字符，可能是扫描件、空白报告或解析异常。",
                "确认报告内容完整；若为扫描件，请提供可检索文字版本。",
            )
        )
    if page_count is not None and page_count <= 2:
        findings.append(
            _finding(
                "COMMON-009",
                "warning",
                "报告页数异常偏少",
                f"报告共 {page_count} 页。",
                "确认封面、目录、巡检内容、结论及汇总表是否齐全。",
            )
        )
    return findings


def _extract_pdf(content: bytes) -> tuple[str, dict]:
    import pymupdf

    document = pymupdf.open(stream=content, filetype="pdf")
    pages = []
    for index, page in enumerate(document):
        pages.append(f"\n[第{index + 1}页]\n{page.get_text()}")
    meta = {
        "file_type": "pdf",
        "page_count": document.page_count,
        "text_length": sum(len(page) for page in pages),
    }
    document.close()
    return "".join(pages), meta


def _extract_docx(content: bytes) -> tuple[str, dict]:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    parts = []
    with zipfile.ZipFile(BytesIO(content)) as archive:
        names = ["word/document.xml"] + sorted(
            name for name in archive.namelist() if re.match(r"word/(header|footer)\d+\.xml$", name)
        )
        for name in names:
            if name not in archive.namelist():
                continue
            root = ElementTree.fromstring(archive.read(name))
            chunks = [node.text or "" for node in root.findall(".//w:t", namespace)]
            parts.append("\n".join(chunks))
    text = "\n".join(parts)
    return text, {"file_type": "docx", "page_count": None, "text_length": len(text)}


def _clean_json_block(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON 对象")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型结果不是 JSON 对象")
    return data


def _run_llm_review(customer: str, product: str, filename: str, text: str) -> tuple[list[dict], str]:
    settings = get_settings()
    if not settings.report_audit_ai_enabled or not settings.ai_api_key:
        return [], "大模型审核未启用或未配置密钥，仅执行硬规则。"

    from zhipuai import ZhipuAI

    prompt = f"""你是企业安全产品巡检报告审核员。请只依据下方已提取的报告文字审核，禁止猜测字体、图片清晰度、页眉图案等无法由文字证明的事项。

派单客户：{customer}
派单产品：{product}
审核文件：{filename}

审核清单：
1. 客户全称与派单、封面、正文一致，不得残留其他客户名称。
2. 产品名称与派单一致；雷池、洞鉴、谛听、牧云等模板不得混用。
3. 标题、目录、章节结构完整；目录标题、层级和正文一致，不得有目录报错占位文字。
4. 巡检日期、时间、版本、设备数量、异常数量、工程师在全文前后一致，不得保留旧日期或模板占位符。
5. 检查结果使用“正常/异常/未涉及”；结论必须与告警、异常和建议一致，异常必须给出可执行建议。
6. 非牧云报告最后一个业务章节必须是“设备巡检信息汇总”，并包含设备类型、巡检日期、运行状态、巡检时间、事件记录、服务商和工程师；牧云除外。
7. 不得出现“绝对安全”等无证据的绝对化结论。

只报告有直接原文证据的问题。证据不足时不要报错。相同问题合并为一条。返回严格 JSON，不要 Markdown：
{{
  "summary": "一句话审核结论",
  "findings": [
    {{
      "rule_id": "AI-001",
      "severity": "blocker|error|warning|info",
      "title": "问题标题",
      "evidence": "原文证据，尽量包含[第N页]标记附近的原文",
      "suggestion": "具体修改建议",
      "page": 1
    }}
  ]
}}

报告文字：
{text[:60000]}
"""
    client = ZhipuAI(
        api_key=settings.ai_api_key,
        timeout=max(5.0, settings.report_audit_ai_timeout_seconds),
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=settings.report_audit_ai_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    data = _clean_json_block(response.choices[0].message.content)
    findings = []
    for index, raw in enumerate(data.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity") or "warning").lower()
        if severity not in SEVERITY_WEIGHT:
            severity = "warning"
        findings.append(
            _finding(
                str(raw.get("rule_id") or f"AI-{index + 1:03d}"),
                severity,
                str(raw.get("title") or "语义审核问题"),
                str(raw.get("evidence") or "模型未给出证据"),
                str(raw.get("suggestion") or "请人工复核"),
                source="ai",
                page=raw.get("page") if isinstance(raw.get("page"), int) else None,
            )
        )
    return findings, str(data.get("summary") or "大模型审核完成")[:2000]


def _safe_attachments(attachments: list[dict]) -> list[dict]:
    """Persist metadata only; never store expiring signed download URLs."""
    return [
        {
            "filename": attachment.get("filename") or attachment.get("name") or "",
            "resource_id": attachment.get("resourceId") or attachment.get("fileId") or attachment.get("id") or "",
            "size": attachment.get("size") or attachment.get("fileSize") or 0,
            "type": attachment.get("type") or "",
        }
        for attachment in attachments
        if isinstance(attachment, dict)
    ]


def _choose_attachment(attachments: list[dict]) -> dict | None:
    candidates = [
        attachment
        for attachment in attachments
        if isinstance(attachment, dict)
        and str(attachment.get("filename") or "").lower().endswith(SUPPORTED_EXTENSIONS)
    ]
    candidates.sort(
        key=lambda attachment: (
            not str(attachment.get("filename") or "").lower().endswith(".pdf"),
            str(attachment.get("filename") or ""),
        )
    )
    return candidates[0] if candidates else None


async def _download_attachment(attachment: dict) -> bytes:
    settings = get_settings()
    url = attachment.get("url") or attachment.get("downloadUrl") or attachment.get("resourceUrl") or ""
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise RuntimeError("附件缺少有效下载地址")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
    max_bytes = settings.report_audit_max_file_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise RuntimeError(f"附件超过大小限制（{settings.report_audit_max_file_mb}MB）")
    return content


def _finalize(row: ReportAudit, findings: list[dict]) -> None:
    row.findings = findings
    row.blocker_count = sum(1 for item in findings if item.get("severity") == "blocker")
    row.error_count = sum(1 for item in findings if item.get("severity") == "error")
    row.warning_count = sum(1 for item in findings if item.get("severity") == "warning")
    row.score = max(0, 100 - sum(SEVERITY_WEIGHT.get(str(item.get("severity")), 5) for item in findings))
    if row.blocker_count:
        row.status = "rejected"
    elif row.error_count or row.warning_count:
        row.status = "warning"
    else:
        row.status = "passed"
    row.reviewed_at = datetime.now(timezone.utc)
    row.error_message = None


async def review_audit(db: Session, row: ReportAudit, attachment: dict, attachments: list[dict]) -> None:
    row.status = "running"
    row.error_message = None
    db.commit()
    try:
        content = await _download_attachment(attachment)
        row.content_sha256 = hashlib.sha256(content).hexdigest()
        filename = str(attachment.get("filename") or "report")
        if filename.lower().endswith(".pdf"):
            text, meta = _extract_pdf(content)
        elif filename.lower().endswith(".docx"):
            text, meta = _extract_docx(content)
        else:
            raise RuntimeError("暂只支持 PDF 和 DOCX 报告")
        row.document_meta = meta
        findings = run_attachment_rules(attachments)
        findings.extend(
            run_hard_rules(row.customer_name or "", row.product_name or "", filename, text, meta.get("page_count"))
        )
        try:
            settings = get_settings()
            ai_findings, summary = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_llm_review,
                    row.customer_name or "",
                    row.product_name or "",
                    filename,
                    text,
                ),
                timeout=max(10.0, settings.report_audit_ai_timeout_seconds + 5.0),
            )
            row.ai_used = bool(settings.report_audit_ai_enabled and settings.ai_api_key)
            row.llm_summary = summary
            findings.extend(ai_findings)
        except Exception as exc:
            logger.warning("Report audit AI failed for %s: %s", row.aitable_record_id, exc)
            row.ai_used = False
            detail = "调用超时" if isinstance(exc, TimeoutError) else (str(exc)[:300] or exc.__class__.__name__)
            row.llm_summary = f"大模型审核失败，仅保留硬规则结果：{detail}"
        _finalize(row, findings)
        db.commit()
    except Exception as exc:
        logger.exception("Report audit failed for record %s", row.aitable_record_id)
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.reviewed_at = datetime.now(timezone.utc)
        db.commit()


async def scan_reports(db: Session, *, limit: int | None = None, force_record_id: str | None = None) -> dict:
    """Audit new unsent report versions without any external write-back."""
    async with _SCAN_LOCK:
        settings = get_settings()
        max_items = max(1, min(limit or settings.report_audit_scan_limit, 20))
        records = await dingtalk_client.query_records(
            limit=100,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
            fetch_all=True,
            strict=True,
        )
        scanned = created = reviewed = skipped = failed = 0
        for record in records:
            if reviewed >= max_items:
                break
            record_id = record.get("recordId") or record.get("record_id") or ""
            if force_record_id and record_id != force_record_id:
                continue
            cells = record.get("cells") or {}
            attachments = cells.get(DISPATCH["巡检报告"])
            if not record_id or not isinstance(attachments, list) or not attachments:
                continue
            # New reports are audited before outbound mail. A forced recheck is
            # allowed after mail was sent so an existing result remains usable.
            if not force_record_id and extract_select_name(cells.get(DISPATCH["邮件是否发送"])) == "是":
                continue
            scanned += 1
            attachment = _choose_attachment(attachments)
            if not attachment:
                skipped += 1
                continue
            fingerprint = attachment_fingerprint(attachments)
            row = (
                db.query(ReportAudit)
                .filter(
                    ReportAudit.aitable_record_id == record_id,
                    ReportAudit.attachment_fingerprint == fingerprint,
                    ReportAudit.rule_version == RULE_VERSION,
                )
                .first()
            )
            if row and not force_record_id and row.status not in {"pending", "running", "failed"}:
                skipped += 1
                continue
            if row:
                row.status = "pending"
                row.score = None
                row.blocker_count = 0
                row.error_count = 0
                row.warning_count = 0
                row.findings = None
                row.document_meta = None
                row.llm_summary = None
                row.ai_used = False
                row.error_message = None
            else:
                row = ReportAudit(
                    aitable_record_id=record_id,
                    pts_order_id=extract_pts_order_id_from_link(cells.get(DISPATCH["巡检工单链接"])),
                    customer_name=extract_text(cells.get(DISPATCH["客户名称"])),
                    product_name=extract_text(cells.get(DISPATCH["产品名称"])),
                    filename=str(attachment.get("filename") or "report"),
                    attachment_fingerprint=fingerprint,
                    attachments=_safe_attachments(attachments),
                    rule_version=RULE_VERSION,
                    status="pending",
                )
                db.add(row)
                created += 1
            db.commit()
            await review_audit(db, row, attachment, attachments)
            reviewed += 1
            if row.status == "failed":
                failed += 1
        return {
            "scanned": scanned,
            "created": created,
            "reviewed": reviewed,
            "skipped": skipped,
            "failed": failed,
            "rule_version": RULE_VERSION,
            "read_only": True,
        }
