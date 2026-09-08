"""规则 8：售后有效服务期偏差校验"""

from __future__ import annotations

from datetime import date, timedelta

from services.review.audit.schemas import DeliveryItem, ProductInfo, ProductType, RuleResult
from services.review.audit.product_type import (
    SAAS_KEYWORDS,
    extract_short_product_name,
    is_operation_service_item,
)
from services.review.audit.service_package import parse_service_packages, calculate_total_months


def rule8_service_period(
    delivery_items: list[DeliveryItem],
    products: list[ProductInfo],
    approval_time: str | None,
) -> RuleResult:
    all_saas = products and all(p.type == ProductType.saas for p in products)
    if all_saas:
        return _check_saas(products, delivery_items)

    # 审核通过时间只用于“新购”产品服务期起算；纯续保/SaaS/运营服务项目不依赖它
    if not approval_time and _needs_approval_time(delivery_items):
        return RuleResult(
            rule_id=8, rule_name="售后有效服务期偏差", result="无法判定",
            message="无法获取审核通过时间",
        )

    return _check_hw_sw(delivery_items, products, approval_time)


def _add_months(d: date, months: int) -> date:
    """Add months to a date (handles month overflow like JS setMonth)."""
    total_months = d.year * 12 + d.month - 1 + months
    y = total_months // 12
    m = total_months % 12 + 1
    # clamp day to max for resulting month
    max_day = (date(y, m + 1, 1) - timedelta(days=1)).day if m < 12 else (date(y + 1, 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, max_day))


def _parse_date(s: str) -> date | None:
    import re
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _format_date(d: date) -> str:
    return d.isoformat()


def _is_date_in_range(date_str: str, range_start: date, range_end: date) -> bool:
    d = _parse_date(date_str)
    return d is not None and range_start <= d <= range_end


def _is_renewal_item(product_category: str) -> bool:
    """判断是否为续保交付项（form 名包含"续保"）"""
    parts = product_category.split("-")
    return "续保" in (parts[-1] if parts else "")


def _is_rental_or_subscription_item(product_category: str) -> bool:
    parts = product_category.split("-")
    form_name = parts[-1] if parts else ""
    return "订阅" in form_name or "租用" in form_name


def _is_saas_or_service_item(product_category: str) -> bool:
    """SaaS/运营服务类交付项：下单的是服务而非产品，无售后有效服务期校验。"""
    if is_operation_service_item(product_category):
        return True
    return any(kw in product_category for kw in SAAS_KEYWORDS)


def _category_prefix(product_category: str) -> str:
    """产品名前缀：去掉形态后缀（-软件版/-硬件版/-续保 等）"""
    parts = product_category.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else product_category


def _needs_approval_time(delivery_items: list[DeliveryItem]) -> bool:
    """是否存在需要以审核通过时间起算服务期的“新购”产品。"""
    for item in delivery_items:
        category = item.product_category or ""
        if _is_renewal_item(category) or _is_saas_or_service_item(category):
            continue
        return True
    return False


def _get_service_period_range(approval_date_str: str, total_months: int, deviation: int = 6) -> tuple[date, date]:
    approval = _parse_date(approval_date_str)
    if approval is None:
        raise ValueError(f"Cannot parse approval date: {approval_date_str}")
    expected = _add_months(approval, total_months)
    range_start = _add_months(expected, -deviation)
    range_end = _add_months(expected, deviation)
    return range_start, range_end


def _get_service_period_range_from_activation(
    after_sales_service_period: str, total_months: int, deviation: int = 6,
) -> tuple[date, date]:
    """续保项目：以 after_sales_service_period 反推的激活日期作为起算点

    续保项目的售后服务期从实际交付激活日开始算，而非合同审批日。
    激活日 = after_sales_service_period - total_months，然后以激活日为起算点
    计算预期结束日和允许范围，避免交付周期导致误判。
    """
    end_date = _parse_date(after_sales_service_period)
    if end_date is None:
        raise ValueError(f"Cannot parse after_sales_service_period: {after_sales_service_period}")
    activation = _add_months(end_date, -total_months)
    expected = _add_months(activation, total_months)  # == end_date (approximately)
    range_start = _add_months(expected, -deviation)
    range_end = _add_months(expected, deviation)
    return range_start, range_end


def _check_saas(products: list[ProductInfo], delivery_items: list[DeliveryItem]) -> RuleResult:
    is_operation = any(
        parse_service_packages(ci.text).is_operation_service
        for item in delivery_items for ci in item.config_items
    )
    if is_operation:
        return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="通过", message="运营服务类，售后有效服务期允许为空")

    empty = [p for p in products if not p.after_sales_service_period or p.after_sales_service_period == "---"]
    if empty:
        return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="不通过", message=f"{len(empty)}个产品无售后有效服务期")

    return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="通过", message="SaaS/服务产品，售后有效服务期均不为空")


def _check_hw_sw(delivery_items: list[DeliveryItem], products: list[ProductInfo], approval_time: str | None) -> RuleResult:
    non_saas = [p for p in products if p.type != ProductType.saas]
    details: list[str] = []
    all_pass = True
    has_undetermined = False

    # 按交付商品类精确分组
    # 续保交付项品类为 X-续保，而产品详情品类为 X-软件版/X-硬件版，
    # 精确匹配不到时按产品名前缀回退匹配
    by_type: dict[str, list[ProductInfo]] = {}
    by_prefix: dict[str, list[ProductInfo]] = {}
    for p in non_saas:
        by_type.setdefault(p.product_category, []).append(p)
        by_prefix.setdefault(_category_prefix(p.product_category), []).append(p)

    consumed: dict[str, int] = {}

    for item in delivery_items:
        category = item.product_category or ""
        if _is_saas_or_service_item(category):
            details.append(f"{extract_short_product_name(category)}: SaaS/服务类，豁免")
            continue

        is_renewal = _is_renewal_item(category)
        is_rental = _is_rental_or_subscription_item(category)

        matching = by_type.get(category, [])
        key = category
        if not matching and is_renewal:
            prefix = _category_prefix(category)
            matching = by_prefix.get(prefix, [])
            key = prefix

        for ci in item.config_items:
            parsed = parse_service_packages(ci.text)
            total_months = calculate_total_months(parsed)

            qty = ci.quantity or 1
            offset = consumed.get(key, 0)
            products_for = matching[offset:offset + qty]
            consumed[key] = offset + qty

            if total_months == 0 and is_rental:
                # 租用/订阅版未带显式年限时按 1 年计
                total_months = 12

            if total_months == 0 and not parsed.is_operation_service:
                has_undetermined = True
                details.append(f"{extract_short_product_name(category)}: 未找到服务包年限")
                continue

            if total_months == 0 and parsed.is_operation_service:
                details.append(f"{extract_short_product_name(category)}: 运营服务类，豁免")
                continue

            for product in products_for:
                period = product.after_sales_service_period
                if not period or period == "---":
                    all_pass = False
                    details.append(f"{extract_short_product_name(product.product_category)}: 无售后有效服务期")
                    continue

                # 续保项目：以 after_sales_service_period 反推的激活日期为起算点
                # 避免因交付周期长导致审批日与激活日之间的偏差被误判
                if is_renewal:
                    range_start, range_end = _get_service_period_range_from_activation(period, total_months)
                else:
                    range_start, range_end = _get_service_period_range(approval_time, total_months)

                if _is_date_in_range(period, range_start, range_end):
                    details.append(f"{extract_short_product_name(product.product_category)}: 服务期 {period} 在 [{_format_date(range_start)}, {_format_date(range_end)}] 范围内")
                else:
                    all_pass = False
                    details.append(f"{extract_short_product_name(product.product_category)}: 服务期偏差")

    if not all_pass:
        return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="不通过", message="; ".join(details))
    if has_undetermined:
        return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="无法判定", message="; ".join(details))

    return RuleResult(rule_id=8, rule_name="售后有效服务期偏差", result="通过", message="; ".join(details) or "校验通过")
