"""规则 4：识别配置项关键字段"""

from services.review.audit.schemas import DeliveryItem, ProductInfo, ProductType, RuleResult
from services.review.audit.product_type import (
    SAAS_KEYWORDS,
    extract_short_product_name,
    is_operation_service_item,
)
from services.review.audit.service_package import parse_service_packages


def _is_saas_or_service_item(product_category: str) -> bool:
    """SaaS/运营服务类交付项：下单的是服务而非产品，不承载服务包年限。"""
    if is_operation_service_item(product_category):
        return True
    return any(kw in product_category for kw in SAAS_KEYWORDS)


def _is_rental_or_subscription_item(product_category: str) -> bool:
    parts = product_category.split("-")
    form_name = parts[-1] if parts else ""
    return "订阅" in form_name or "租用" in form_name


def rule4_config_keywords(delivery_items: list[DeliveryItem], products: list[ProductInfo]) -> RuleResult:
    all_saas = products and all(p.type == ProductType.saas for p in products)

    if all_saas:
        return RuleResult(
            rule_id=4, rule_name="识别配置项关键字段", result="通过",
            message="SaaS/服务产品，配置项不为空即可",
        )

    saas_items = [item for item in delivery_items if _is_saas_or_service_item(item.product_category)]
    device_items = [item for item in delivery_items if not _is_saas_or_service_item(item.product_category)]

    # 交付项全部为 SaaS/运营服务：配置项不为空即可（非空由规则3校验）
    if not device_items and saas_items:
        return RuleResult(
            rule_id=4, rule_name="识别配置项关键字段", result="通过",
            message="SaaS/服务产品，配置项不为空即可",
        )

    all_results: list[str] = []
    has_service_package = False
    has_unrecognized = False

    for item in device_items:
        is_rental = _is_rental_or_subscription_item(item.product_category)
        for ci in item.config_items:
            parsed = parse_service_packages(ci.text)
            total_months = parsed.years * 12 + parsed.months

            if total_months > 0:
                has_service_package = True
                months_str = f"+{parsed.months}月" if parsed.months else ""
                all_results.append(
                    f"{extract_short_product_name(item.product_category)}: {parsed.years}年{months_str} ({total_months}个月)"
                )
            elif is_rental:
                # 租用/订阅版未带显式年限时按 1 年计
                has_service_package = True
                all_results.append(f"{extract_short_product_name(item.product_category)}: 订阅/租用1年 (12个月)")
            elif parsed.unrecognized:
                has_unrecognized = True
                all_results.append(f"未识别: {', '.join(parsed.unrecognized)}")

    if has_service_package:
        return RuleResult(
            rule_id=4, rule_name="识别配置项关键字段", result="通过",
            message="; ".join(all_results),
        )

    if has_unrecognized:
        return RuleResult(
            rule_id=4, rule_name="识别配置项关键字段", result="无法判定",
            message="未匹配到服务包年限",
        )

    return RuleResult(
        rule_id=4, rule_name="识别配置项关键字段", result="无法判定",
        message="未找到服务包年限",
    )
