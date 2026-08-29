"""交付转售后审核 — 全流程编排服务

编排: 获取待审核项目 → 逐项采集详情 → 运行审核引擎 → 钉钉写入 → PTS 回写
遵循 inspection-workflow 的 Service 模式（顶层 async def，无类依赖）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from core.config import get_settings
from models.review_audit_log import ReviewAuditLog
from services.review.audit.engine import run_audit
from services.review.audit.product_type import is_key_product, extract_short_product_name
from services.review.audit.schemas import AuditInput, AuditResult
from services.review.extractors.review_approval_time import extract_approval_time
from services.review.extractors.review_main_page import extract_project_data
from services.review.extractors.review_product_detail import extract_product_detail
from services.review.extractors.review_project_list import extract_pending_projects
from services.review.pts_review_client import (
    fetch_company_by_id,
    fetch_release_licenses,
    submit_review,
    update_product_info_license_id,
)
from services.review.review_dingtalk import (
    compute_delivery_type,
    compute_project_type,
    compute_region,
    write_audit_to_dingtalk,
)

logger = logging.getLogger(__name__)


async def run_review_pipeline(
    db: Session,
    *,
    trigger_source: str = "manual",
) -> dict[str, Any]:
    """执行一次完整的交付转售后审核流水线。

    Args:
        db: SQLAlchemy Session
        trigger_source: 触发来源 (manual/scheduler)

    Returns:
        汇总结果 dict
    """
    settings = get_settings()

    # 前置检查
    if not settings.review_real_execution_enabled:
        return {
            "status": "skipped",
            "reason": "review_real_execution_enabled 未启用",
        }

    # Step 1: 获取待审核项目列表
    try:
        after_sale_ids = None
        if settings.pts_review_after_sale_filter_ids:
            after_sale_ids = [
                s.strip()
                for s in settings.pts_review_after_sale_filter_ids.split(",")
                if s.strip()
            ]

        projects = await extract_pending_projects(after_sale_ids=after_sale_ids)
    except Exception as exc:
        logger.exception("获取待审核项目列表失败")
        return {"status": "error", "reason": f"获取待审核项目列表失败: {exc}"}

    if not projects:
        logger.info("当前无待审核项目")
        return {"status": "success", "total": 0, "results": []}

    logger.info("开始审核流水线: total=%d, trigger_source=%s", len(projects), trigger_source)

    results: list[dict[str, Any]] = []
    summary = {"total": len(projects), "passed": 0, "rejected": 0, "manual": 0, "errors": 0}

    for project in projects:
        try:
            result = await audit_single_project(project.project_id)
            # 保存审核日志
            _save_audit_log(db, project.project_id, project.project_name, project.customer_name, result, trigger_source)
            db.commit()

            # 汇总
            if result.get("error"):
                summary["errors"] += 1
            elif result.get("conclusion") == "通过":
                summary["passed"] += 1
            elif result.get("conclusion") == "不通过":
                summary["rejected"] += 1
            else:
                summary["manual"] += 1

            results.append(result)

        except Exception as exc:
            logger.exception("审核项目失败: project_id=%s", project.project_id)
            summary["errors"] += 1
            results.append({
                "project_id": project.project_id,
                "project_name": project.project_name,
                "customer_name": project.customer_name,
                "error": str(exc),
            })
            # 尝试保存错误日志
            try:
                log = ReviewAuditLog(
                    project_id=project.project_id,
                    project_name=project.project_name,
                    customer_name=project.customer_name,
                    conclusion="error",
                    trigger_source=trigger_source,
                    error=str(exc),
                )
                db.add(log)
                db.commit()
            except Exception:
                db.rollback()

    logger.info(
        "审核流水线完成: total=%d, passed=%d, rejected=%d, manual=%d, errors=%d",
        summary["total"], summary["passed"], summary["rejected"], summary["manual"], summary["errors"],
    )

    return {"status": "success", **summary, "results": results}


async def audit_single_project(project_id: str, *, skip_rules: set[int] | None = None) -> dict[str, Any]:
    """对单个项目执行完整审核流程。

    Args:
        project_id: PTS 项目 ID
        skip_rules: 跳过的规则 ID 集合（如 {1} 跳过规则1门控）
    """
    # 前置检查
    settings = get_settings()
    if not settings.review_real_execution_enabled:
        return {
            "project_id": project_id,
            "error": "review_real_execution_enabled 未启用",
            "conclusion": "error",
        }

    # Step 1: 采集项目详情
    try:
        project_data = await extract_project_data(project_id)
    except Exception as exc:
        logger.error("采集项目详情失败: project_id=%s, error=%s", project_id, exc)
        return {"project_id": project_id, "error": f"采集项目详情失败: {exc}"}

    # 从项目关联公司查询客户销售。销售查询失败不阻断审核，但不以空值覆盖钉钉已有字段。
    if not project_data.company_id:
        project_data.sales_lookup_status = "missing_company_id"
    else:
        company_data = await fetch_company_by_id(project_data.company_id)
        company = (company_data or {}).get("CompanyByID") if company_data else None
        claim_by = (company or {}).get("claim_by") if isinstance(company, dict) else None
        if isinstance(claim_by, dict) and claim_by.get("name"):
            project_data.sales_name = claim_by.get("name")
            project_data.sales_pts_id = claim_by.get("id")
            project_data.sales_pts_username = claim_by.get("username")
            project_data.sales_lookup_status = "found"
        elif company_data is None:
            project_data.sales_lookup_status = "lookup_error"
        else:
            project_data.sales_lookup_status = "not_found"

    # 获取产品详情
    product_details = []
    for product in project_data.products:
        try:
            detail = await extract_product_detail(product.product_id)
            if detail:
                product_details.append(detail)
        except Exception as exc:
            logger.warning("获取产品详情失败: product_id=%s, error=%s", product.product_id, exc)

    # 续保标记：根据 delivery_items 的 product_category 中的 "-续保" 后缀检测
    # 只对无真实设备信息的产品实例做续保匹配，
    # 有真实设备信息（序列号/机器码/型号任一有值）的产品是实际设备，即使续保交付项引用了它们也不应标记为续保记录。
    # 这修复了纯续保项目中所有产品实例被错误标记为续保、导致规则6判定"缺产品实例"的问题。
    renewal_prefixes: set[str] = set()
    for di in project_data.delivery_items:
        parts = (di.product_category or "").rsplit("-", 1)
        if len(parts) == 2 and parts[-1] == "续保":
            renewal_prefixes.add(parts[0])
    for detail in product_details:
        if detail.is_renewal_record:
            continue
        # 有真实设备信息的产品是实际设备，跳过续保标记
        if _has_real_device_info(detail):
            continue
        parts = (detail.product_category or "").rsplit("-", 1)
        if len(parts) == 2 and parts[0] in renewal_prefixes:
            detail.is_renewal_record = True

    # License ID 自动补全：产品 License ID 为空但机器码关联的 License 中，
    # 最新一条的性质与产品填写的 License 性质对应时，自动补全 License ID。
    settings = get_settings()
    license_autofill_logs: list[dict[str, Any]] = []
    if settings.review_license_autofill_enabled:
        for detail in product_details:
            try:
                filled = await _autofill_license_id(detail)
                if filled:
                    license_autofill_logs.append(filled)
            except Exception as exc:
                logger.warning("License ID 自动补全失败: product_id=%s, error=%s", detail.product_id, exc)

    # 自动补全成功后写回 PTS 产品表单（受开关控制）
    if settings.review_license_autofill_writeback:
        for log_item in license_autofill_logs:
            try:
                ok = await update_product_info_license_id(
                    log_item["product_id"],
                    log_item["license_id"],
                    license_nature=log_item.get("filled_license_type") or None,
                )
                log_item["writeback"] = "ok" if ok else "failed"
            except Exception as exc:
                logger.warning("License ID 写回 PTS 失败: product_id=%s, error=%s", log_item["product_id"], exc)
                log_item["writeback"] = f"error: {exc}"

    # 获取审批时间
    approval_time = None
    try:
        approval_time = await extract_approval_time(project_id)
    except Exception as exc:
        logger.warning("获取审批时间失败: project_id=%s, error=%s", project_id, exc)

    # Step 2: 构建审核输入并运行审核引擎
    audit_input = AuditInput(
        project_id=project_data.project_id,
        project_name=project_data.project_name,
        customer_name=project_data.customer_name,
        company_id=project_data.company_id,
        crm_project_id=project_data.crm_project_id,
        sales_name=project_data.sales_name,
        sales_pts_id=project_data.sales_pts_id,
        sales_pts_username=project_data.sales_pts_username,
        sales_lookup_status=project_data.sales_lookup_status,
        delivery_stage=project_data.delivery_stage,
        stage_status=project_data.stage_status,
        after_sales_leader=project_data.after_sales_leader,
        assigner_username=project_data.assigner_username,
        assigner_name=project_data.assigner_name,
        person_in_charge_username=project_data.person_in_charge_username,
        person_in_charge_name=project_data.person_in_charge_name,
        delivery_items=project_data.delivery_items,
        contacts=project_data.contacts,
        products=project_data.products,
        product_details=product_details,
        approval_time=approval_time,
        partner_delivery_type=project_data.partner_delivery_type,
    )

    audit_result = run_audit(audit_input, skip_rules=skip_rules)

    # Step 3: 计算区域/交付类型/项目类型
    try:
        audit_result.region = compute_region(audit_result.assigner_name)
        audit_result.delivery_type = compute_delivery_type(
            audit_input.delivery_items, product_details, audit_input.partner_delivery_type,
        )
        audit_result.project_type = compute_project_type(audit_input.delivery_items)
    except Exception as exc:
        logger.warning("区域/类型计算失败: project_id=%s, error=%s", project_id, exc)

    # Step 4: 非关键产品 → 售后有效服务期全部非空则直接通过，否则转人工审核
    _apply_non_key_manual_review(audit_result, product_details)

    # Step 5: 钉钉写入
    settings = get_settings()
    dingtalk_result: dict[str, Any] | None = None
    if settings.review_writeback_enabled:
        try:
            dingtalk_result = await write_audit_to_dingtalk(audit_result)
        except Exception as exc:
            logger.warning("钉钉写入失败: project_id=%s, error=%s", project_id, exc)
            dingtalk_result = {"enabled": True, "error": str(exc)}
    else:
        dingtalk_result = {"enabled": False}

    # Step 6: PTS 回写（仅通过时自动回写，拒绝的项目需人工审核后手动操作）
    pts_review_result: dict[str, Any] | None = None
    if audit_result.conclusion == "通过":
        reason = _build_pts_review_reason(audit_result)
        pts_review_result = await submit_review(project_id, True, reason)
    elif audit_result.conclusion == "不通过":
        pts_review_result = {"skipped": True, "reason": "拒绝项目需人工审核后手动操作"}

    return {
        "project_id": project_id,
        "project_name": audit_result.project_name,
        "customer_name": audit_result.customer_name,
        "company_id": audit_result.company_id,
        "crm_project_id": audit_result.crm_project_id,
        "sales_name": audit_result.sales_name,
        "sales_pts_id": audit_result.sales_pts_id,
        "sales_pts_username": audit_result.sales_pts_username,
        "sales_lookup_status": audit_result.sales_lookup_status,
        "conclusion": audit_result.conclusion,
        "region": audit_result.region,
        "delivery_type": audit_result.delivery_type,
        "project_type": audit_result.project_type,
        "rules": [
            {"rule_id": r.rule_id, "rule_name": r.rule_name, "result": r.result, "message": r.message}
            for r in audit_result.rules
        ],
        "license_autofill": license_autofill_logs,
        "dingtalk_writeback": dingtalk_result,
        "pts_review_writeback": pts_review_result,
    }


def _build_pts_review_reason(result: AuditResult) -> str:
    """构建 PTS 审核回写的原因文本。"""
    if result.conclusion == "通过":
        return "自动审核通过"
    parts: list[str] = []
    for r in result.rules:
        if r.result in ("不通过", "无法判定"):
            parts.append(f"规则{r.rule_id}({r.rule_name}): {r.message}")
    return "自动审核不通过\n" + "\n".join(parts) if parts else "自动审核不通过"


def _save_audit_log(
    db: Session,
    project_id: str,
    project_name: str | None,
    customer_name: str | None,
    result: dict[str, Any],
    trigger_source: str,
) -> None:
    """保存审核日志到数据库。"""
    log = ReviewAuditLog(
        project_id=project_id,
        project_name=project_name,
        customer_name=customer_name,
        conclusion=result.get("conclusion", "error"),
        region=result.get("region"),
        delivery_type=result.get("delivery_type"),
        project_type=result.get("project_type"),
        rules_result=result.get("rules"),
        dingtalk_writeback=result.get("dingtalk_writeback"),
        pts_review_writeback=result.get("pts_review_writeback"),
        trigger_source=trigger_source,
        error=result.get("error"),
    )
    db.add(log)


async def list_review_logs(
    db: Session,
    *,
    conclusion: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ReviewAuditLog], int]:
    """查询审核日志列表。"""
    from sqlalchemy import func as sa_func

    query = db.query(ReviewAuditLog)
    count_query = db.query(sa_func.count(ReviewAuditLog.id))

    if conclusion:
        query = query.filter(ReviewAuditLog.conclusion == conclusion)
        count_query = count_query.filter(ReviewAuditLog.conclusion == conclusion)

    total = count_query.scalar() or 0
    items = (
        query.order_by(ReviewAuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return items, total


# ── 续保标记辅助函数 ────────────────────────────────────────

_PLACEHOLDER_VALUES = {"1", "2", "NA", "N/A", "无", "---", "", "undefined", "null"}


def _has_valid_after_sales_period(detail) -> bool:
    """售后有效服务期是否非空（排除占位符）。"""
    period = (detail.after_sales_service_period or "").strip()
    return bool(period) and period not in _PLACEHOLDER_VALUES


def _apply_non_key_manual_review(audit_result: AuditResult, product_details: list) -> None:
    """非关键产品兜底：无关键产品且规则全通过时，售后有效服务期全部非空则直接通过，否则转人工审核。"""
    if audit_result.conclusion != "通过":
        return
    if any(is_key_product(p) for p in product_details):
        return

    missing_period_products = [
        extract_short_product_name(p.product_category or "") or p.product_id
        for p in product_details
        if not _has_valid_after_sales_period(p)
    ]
    if not product_details or missing_period_products:
        audit_result.conclusion = "转人工审核"
        product_names = {extract_short_product_name(p.product_category or "") for p in product_details} or {"未知产品"}
        audit_result.manual_review_reason = (
            "非关键产品（" + "、".join(sorted(product_names))
            + "）售后有效服务期缺失，需人工确认"
        )


def _has_real_device_info(detail) -> bool:
    """判断产品是否有真实设备信息（序列号/机器码/型号任一有实际值）

    续保项目的产品实例虽然 delivery_item 的 form 是"续保"，
    但产品详情的 after_info 中有真实的序列号、机器码、型号等信息，
    说明这是实际存在的设备，不应被标记为续保记录。
    """
    from services.review.audit.schemas import ProductInfo

    if not isinstance(detail, ProductInfo):
        return False

    for value in (detail.serial_number, detail.machine_code, detail.model):
        if value and value.strip() not in _PLACEHOLDER_VALUES:
            return True
    return False


# ── License 信息自动补全辅助函数 ─────────────────────────────────

# 产品 License 性质 → 机器码关联 License 的 (purpose, permanent_license) 匹配键
# 注意: permanent（永久）等同于正式交付-永久
_LICENSE_NATURE_TO_PURPOSE_PERMANENT: dict[str, tuple[str, bool]] = {
    "permanent": ("formal_delivery", True),
    "正式交付-永久": ("formal_delivery", True),
    "formal_delivery_permanent": ("formal_delivery", True),
    "poc_not_permanent": ("poc", False),
    "正式交付-非永久": ("formal_delivery", False),
    "formal_delivery_not_permanent": ("formal_delivery", False),
    "formal_delivery_non_permanent": ("formal_delivery", False),
    "formal_delivery_non_permanent_license": ("formal_delivery", False),
}

# 机器码关联 License (purpose, permanent_license) → 可补全的 License 性质
# 仅包含允许自动补全的 4 类性质（permanent 归一到 formal_delivery_permanent）
_PURPOSE_PERMANENT_TO_LICENSE_NATURE: dict[tuple[str, bool], str] = {
    ("formal_delivery", True): "formal_delivery_permanent",
    ("formal_delivery", False): "formal_delivery_not_permanent",
    ("poc", False): "poc_not_permanent",
}

_REVOKED_STATUSES = {"revoked", "revoking"}

_MACHINE_CODE_SEPARATORS = re.compile(r"[、，,;\s]+")


def _split_machine_codes(raw: str) -> list[str]:
    """拆分多机器码（顿号/中文逗号/英文逗号/分号/空白分隔），返回去空后的列表。"""
    return [mc.strip() for mc in _MACHINE_CODE_SEPARATORS.split(raw) if mc.strip()]


async def _autofill_license_id(detail) -> dict[str, Any] | None:
    """按机器码关联的 License 自动补全产品 License 信息（性质 + ID）。

    - 产品已填 License 性质：要求其与机器码关联 License 中最新一条的性质对应，
      仅补全 License ID；
    - 产品未填 License 性质：从最新一条 License 推导性质，同时补全性质与 License ID。
    仅当 License 性质与 License ID 至少缺一项、存在机器码且找到匹配 License 时补全。

    Returns:
        补全成功返回补全信息 dict（含 product_id/license_type/filled_license_type/license_id），
        否则返回 None。
    """
    if detail.is_renewal_record:
        return None
    # 性质与 License ID 都已填，无需补全
    if (
        detail.license_id and detail.license_id.strip()
        and detail.license_type and detail.license_type.strip()
    ):
        return None

    raw_machine_codes = (detail.machine_code or "").strip()
    if not raw_machine_codes or raw_machine_codes in _PLACEHOLDER_VALUES:
        return None

    # 多机器码（顿号/逗号/空白分隔）逐个查询，取第一个能查到 License 的机器码
    licenses = None
    for machine_code in _split_machine_codes(raw_machine_codes):
        found = await fetch_release_licenses(detail.product_id, machine_code)
        if found:
            licenses = found
            break
    if not licenses:
        return None

    # 过滤已吊销/吊销中的记录，最新一条为列表首条（PTS 默认最新在前）
    active = [lic for lic in licenses if lic.get("revoke_status") not in _REVOKED_STATUSES]
    if not active:
        return None

    latest = active[0]
    purpose = latest.get("purpose")
    permanent = bool(latest.get("permanent_license"))
    derived_nature = _PURPOSE_PERMANENT_TO_LICENSE_NATURE.get((purpose, permanent))
    if derived_nature is None:
        # 最新 License 不属于可自动补全的 4 类性质
        return None

    license_id = (latest.get("license_id") or "").strip()
    if not license_id:
        return None

    current_nature = (detail.license_type or "").strip()
    filled_nature = ""
    if current_nature:
        # 产品已填性质：必须与最新 License 性质对应才补全
        expected = _LICENSE_NATURE_TO_PURPOSE_PERMANENT.get(current_nature)
        if expected is None or (purpose, permanent) != expected:
            return None
    else:
        # 产品未填性质：用最新 License 推导的性质补全
        detail.license_type = derived_nature
        filled_nature = derived_nature

    detail.license_id = license_id
    return {
        "product_id": detail.product_id,
        "product_category": detail.product_category,
        "license_type": detail.license_type,
        "license_id": license_id,
        "filled_license_type": filled_nature,
    }
