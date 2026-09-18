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


def _product_families(value: str) -> set[str]:
    normalized = _normalize(value)
    return {
        family
        for family, aliases in PRODUCT_ALIASES.items()
        if any(_normalize(alias) in normalized for alias in aliases)
    }


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

    expected_families = _product_families(product)
    actual_family = _product_family(filename) or _product_family(text[:8000])
    if expected_families and actual_family and actual_family not in expected_families:
        findings.append(
            _finding(
                "COMMON-003",
                "blocker",
                "报告产品与派单产品不一致",
                f"派单产品识别为“{'、'.join(sorted(expected_families))}”，报告识别为“{actual_family}”。",
                "检查是否误传了其他产品的报告，并重新上传正确附件。",
            )
        )
    elif expected_families and not actual_family:
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

    report_family = actual_family or (next(iter(expected_families)) if len(expected_families) == 1 else None)
    if report_family != "牧云" and "设备巡检信息汇总" not in re.sub(r"\s+", "", text):
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


def _evidence_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value or "", flags=re.UNICODE).lower()


def _extract_dates(values: list[str]) -> set[tuple[int, int, int]]:
    dates: set[tuple[int, int, int]] = set()
    for value in values:
        for year, month, day in re.findall(
            r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?",
            value,
        ):
            dates.add((int(year), int(month), int(day)))
    return dates


def _has_explicit_conclusion_conflict(quotes: list[str]) -> bool:
    """Require an unconditional healthy conclusion and an explicit issue."""
    healthy_phrases = (
        "未发现异常",
        "未发现问题",
        "不存在异常",
        "无任何异常",
        "无任何问题",
        "一切正常",
        "全部正常",
        "整体运行正常",
    )
    issue_phrases = ("存在异常", "接入异常", "故障", "报错", "失败", "非标日志", "严重告警")
    return any(any(phrase in quote for phrase in healthy_phrases) for quote in quotes) and any(
        any(phrase in quote for phrase in issue_phrases) for quote in quotes
    )


def _has_placeholder_or_absolute_claim(quotes: list[str]) -> bool:
    combined = "\n".join(quotes)
    if re.search(
        r"(?:待补充|待填写|请填写|客户名称\s*X{2,}|X{4,}|<[^>]{1,30}>|【(?:填写|待填)[^】]*】)",
        combined,
        re.IGNORECASE,
    ):
        return True
    return any(
        phrase in combined
        for phrase in ("绝对安全", "完全不存在", "永不", "百分之百", "100%", "零风险", "一切正常", "全部正常")
    )


def _different_device_scopes(quotes: list[str]) -> bool:
    """Connected inventory and the devices covered by this visit are not the same metric."""
    has_inventory_scope = any(any(word in quote for word in ("接入", "纳管", "总计", "共计")) for quote in quotes)
    has_visit_scope = any(any(word in quote for word in ("本次巡检", "此次巡检", "巡检了")) for quote in quotes)
    return has_inventory_scope and has_visit_scope


def parse_ai_findings(data: dict, text: str) -> list[dict]:
    """Accept only AI findings backed by verbatim, locatable report quotes."""
    findings = []
    text_key = _evidence_key(text)
    raw_findings = data.get("findings") or []
    if not isinstance(raw_findings, list):
        return findings
    for index, raw in enumerate(raw_findings[:8]):
        if not isinstance(raw, dict):
            continue
        raw_quotes = raw.get("evidence_quotes") or []
        if not isinstance(raw_quotes, list):
            continue
        quotes = []
        for value in raw_quotes[:3]:
            quote = str(value or "").strip()
            quote_key = _evidence_key(quote)
            if len(quote_key) >= 6 and quote_key in text_key:
                quotes.append(quote[:200])
        # A semantic quality issue must be demonstrated by at least two report
        # statements. A single sentence may describe a real operational issue,
        # but that is not by itself a defect in the report.
        if len(quotes) < 2:
            continue
        title = str(raw.get("title") or "语义审核问题")
        # Different display formats of the same calendar date are equivalent.
        if any(keyword in title for keyword in ("日期", "时间")):
            dates = _extract_dates(quotes)
            if dates and len(dates) < 2:
                continue
        # Negative claims such as a missing chapter cannot be proven by quoting
        # chapter names. Deterministic rules own absence checks.
        absence_markers = ("缺失", "未提供", "未给出", "没有提供", "没有给出")
        if any(marker in title for marker in absence_markers):
            if "建议" in title and any(word in "".join(quotes) for word in ("建议", "需要", "优化", "处理")):
                title = "整改建议缺少可执行细节"
            else:
                continue
        if "目录" in title or "章节" in title:
            continue
        conflict_markers = ("矛盾", "冲突", "不一致", "错配")
        if any(marker in title for marker in conflict_markers) and len(quotes) < 2:
            continue
        if "结论" in title and any(marker in title for marker in conflict_markers):
            if not _has_explicit_conclusion_conflict(quotes):
                continue
        if "设备" in title and any(word in title for word in ("数", "数量")) and _different_device_scopes(quotes):
            continue
        if any(word in title for word in ("占位", "绝对化")) and not _has_placeholder_or_absolute_claim(quotes):
            continue
        severity = str(raw.get("severity") or "warning").lower()
        if severity not in SEVERITY_WEIGHT:
            severity = "warning"
        if title == "整改建议缺少可执行细节":
            severity = "warning"
        findings.append(
            _finding(
                str(raw.get("rule_id") or f"AI-{index + 1:03d}"),
                severity,
                title,
                "；".join(f"“{quote}”" for quote in quotes),
                str(raw.get("suggestion") or "请人工复核"),
                source="ai",
                page=raw.get("page") if isinstance(raw.get("page"), int) else None,
            )
        )
    return findings


def deduplicate_ai_findings(hard_findings: list[dict], ai_findings: list[dict]) -> list[dict]:
    hard_ids = {str(item.get("rule_id")) for item in hard_findings}
    duplicate_keywords = {
        "COMMON-001": ("客户", "主体"),
        "COMMON-003": ("产品", "模板混用"),
        "COMMON-005": ("目录",),
        "COMMON-006": ("设备巡检信息汇总", "信息汇总"),
        "COMMON-007": ("占位", "模板残留"),
    }
    kept = []
    for finding in ai_findings:
        title = str(finding.get("title") or "")
        if any(
            rule_id in hard_ids and any(keyword in title for keyword in keywords)
            for rule_id, keywords in duplicate_keywords.items()
        ):
            continue
        kept.append(finding)
    return kept


def _run_llm_review(customer: str, product: str, filename: str, text: str) -> tuple[list[dict], str]:
    settings = get_settings()
    if not settings.report_audit_ai_enabled or not settings.ai_api_key:
        return [], "大模型审核未启用或未配置密钥，仅执行硬规则。"

    from zhipuai import ZhipuAI

    prompt = f"""你是企业安全产品巡检报告审核员。
派单客户：{customer}；派单产品：{product}；文件：{filename}。
只检查可由原文直接证明的事实冲突：日期、版本、设备数、异常数、工程师等同一指标前后不一致；明确写“无异常/一切正常”却又写存在异常；整改建议只有“优化/处理”等空泛表述，缺少具体动作。
不要判断目录或章节缺失、客户或产品错配、模板混用、占位符、设备巡检信息汇总，这些由硬规则处理。接入/纳管设备总数与本次巡检设备数属于不同口径，不视为矛盾；CPU/内存/磁盘正常与日志接入异常属于不同维度，不视为矛盾；同一天的不同日期格式不视为矛盾。
相同问题合并，最多 5 条。evidence_quotes 必须是从报告逐字复制的 2-3 个原文片段，每段 8-120 字，不得解释或改写；无法用成对原文直接证明就不要报告。
返回严格 JSON，不要 Markdown：{{"summary":"一句话结论","findings":[{{"rule_id":"AI-001","severity":"blocker|error|warning|info","title":"问题","evidence_quotes":["原文片段1","原文片段2"],"suggestion":"修改建议","page":1}}]}}。
报告文字：{text[:60000]}
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
        max_tokens=1200,
    )
    data = _clean_json_block(response.choices[0].message.content)
    findings = parse_ai_findings(data, text)
    summary = str(data.get("summary") or "大模型审核完成")[:2000]
    if not findings:
        summary = "AI 未发现具有可定位原文证据的语义问题。"
    return findings, summary


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


def group_report_attachments(attachments: list[dict]) -> list[list[dict]]:
    """Pair same-named PDF/DOCX files and keep different reports separate."""
    groups: dict[str, list[dict]] = {}
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or attachment.get("name") or "").strip()
        if not filename.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        report_key = re.sub(r"\.(?:pdf|docx)$", "", filename, flags=re.IGNORECASE).strip().casefold()
        groups.setdefault(report_key, []).append(attachment)
    return [groups[key] for key in sorted(groups)]


def is_unsent_report_record(record: dict) -> bool:
    """Only reports explicitly marked 邮件是否发送=否 are audit candidates."""
    if not isinstance(record, dict):
        return False
    record_id = record.get("recordId") or record.get("record_id") or ""
    cells = record.get("cells") or {}
    attachments = cells.get(DISPATCH["巡检报告"])
    return bool(
        record_id
        and extract_select_name(cells.get(DISPATCH["邮件是否发送"])) == "否"
        and isinstance(attachments, list)
        and group_report_attachments(attachments)
    )


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
            findings.extend(deduplicate_ai_findings(findings, ai_findings))
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


async def scan_reports(
    db: Session,
    *,
    limit: int | None = None,
    force_record_id: str | None = None,
    force_attachment_fingerprint: str | None = None,
) -> dict:
    """Audit reports explicitly marked unsent without any external write-back."""
    async with _SCAN_LOCK:
        settings = get_settings()
        max_items = max(1, min(limit or settings.report_audit_scan_limit, 200))
        records = await dingtalk_client.query_records(
            limit=100,
            base_id=settings.dt_dispatch_base_id,
            table_id=settings.dt_dispatch_table_id,
            fetch_all=True,
            strict=True,
        )
        if force_record_id:
            candidate_records = [
                record
                for record in records
                if (record.get("recordId") or record.get("record_id") or "") == force_record_id
            ]
        else:
            candidate_records = [record for record in records if is_unsent_report_record(record)]
        candidates: list[tuple[dict, list[dict]]] = []
        for record in candidate_records:
            cells = record.get("cells") or {}
            attachments = cells.get(DISPATCH["巡检报告"])
            if not isinstance(attachments, list):
                continue
            for report_attachments in group_report_attachments(attachments):
                fingerprint = attachment_fingerprint(report_attachments)
                if force_attachment_fingerprint and fingerprint != force_attachment_fingerprint:
                    continue
                candidates.append((record, report_attachments))
        scanned = created = reviewed = skipped = failed = 0
        for record, attachments in candidates:
            if reviewed >= max_items:
                break
            record_id = record.get("recordId") or record.get("record_id") or ""
            cells = record.get("cells") or {}
            if not record_id or not attachments:
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
            if not row:
                current_fingerprints = {
                    attachment_fingerprint(group)
                    for group in group_report_attachments(cells.get(DISPATCH["巡检报告"]) or [])
                }
                # Reuse the pre-grouping row once so existing per-record results
                # become per-report results without leaving duplicate legacy rows.
                row = next(
                    (
                        existing
                        for existing in db.query(ReportAudit)
                        .filter(
                            ReportAudit.aitable_record_id == record_id,
                            ReportAudit.rule_version == RULE_VERSION,
                        )
                        .all()
                        if existing.attachment_fingerprint not in current_fingerprints
                    ),
                    None,
                )
            if row and not force_record_id and row.status not in {"pending", "running", "failed"}:
                skipped += 1
                continue
            if row:
                row.pts_order_id = extract_pts_order_id_from_link(cells.get(DISPATCH["巡检工单链接"]))
                row.customer_name = extract_text(cells.get(DISPATCH["客户名称"]))
                row.product_name = extract_text(cells.get(DISPATCH["产品名称"]))
                row.filename = str(attachment.get("filename") or "report")
                row.attachment_fingerprint = fingerprint
                row.attachments = _safe_attachments(attachments)
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
            "eligible": len(candidates),
            "eligible_records": len(candidate_records),
            "scanned": scanned,
            "created": created,
            "reviewed": reviewed,
            "skipped": skipped,
            "failed": failed,
            "rule_version": RULE_VERSION,
            "read_only": True,
        }
