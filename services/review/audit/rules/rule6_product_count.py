"""规则 6：产品信息关联个数 = 配置项下单套数"""

from services.review.audit.schemas import DeliveryItem, ProductInfo, RuleResult
from services.review.audit.product_type import (
    SAAS_KEYWORDS,
    extract_short_product_name,
    is_operation_service_item,
)


def _is_saas_or_service(category: str) -> bool:
    """SaaS/运营服务类交付项，下单的是服务而非产品，无需校验产品实例个数。"""
    if is_operation_service_item(category):
        return True
    for kw in SAAS_KEYWORDS:
        if kw in category:
            return True
    return False


def rule6_product_count(delivery_items: list[DeliveryItem], product_details: list[ProductInfo]) -> RuleResult:
    # SaaS/运营服务/续保类交付项不用校验产品个数
    non_device_items: list[DeliveryItem] = []
    device_items: list[DeliveryItem] = []
    for item in delivery_items:
        if _is_renewal_item(item.product_category) or _is_saas_or_service(item.product_category):
            non_device_items.append(item)
        else:
            device_items.append(item)

    # 预期数量（按产品名累加配置项套数，仅统计硬件/软件类产品）
    # 谛听产品中"探针"类配置项（如探针-系统、探针-设备）是独立硬件探针，
    # 不需要创建对应产品实例，不计入校验
    config_count: dict[str, int] = {}
    for item in device_items:
        name = extract_short_product_name(item.product_category)
        if item.config_items:
            qty = sum(
                ci.quantity or 1
                for ci in item.config_items
                if "探针" not in (ci.text or "")
            )
            if qty > 0:
                config_count[name] = config_count.get(name, 0) + qty
        else:
            config_count[name] = config_count.get(name, 0) + 1

    # 实际数量（续保记录不算设备）
    product_count: dict[str, int] = {}
    for product in product_details:
        if product.is_renewal_record:
            continue
        name = extract_short_product_name(product.product_category)
        product_count[name] = product_count.get(name, 0) + 1

    # 纯续保/纯SaaS/运营服务项目
    if not device_items:
        if non_device_items:
            skipped_categories = {extract_short_product_name(item.product_category) for item in non_device_items}
            return RuleResult(
                rule_id=6, rule_name="产品个数=配置项套数", result="通过",
                message=f"SaaS/运营服务/续保类项目，无需校验产品实例: {', '.join(sorted(skipped_categories))}",
            )
        return RuleResult(rule_id=6, rule_name="产品个数=配置项套数", result="通过", message="无硬件/软件交付项")

    mismatches: list[str] = []
    for name, expected in config_count.items():
        actual = product_count.get(name, 0)
        if actual != expected:
            mismatches.append(f"{name}套数{expected}但产品{actual}个")

    if mismatches:
        # 如果有 SaaS/运营服务类也被跳过了，在消息中注明
        base_msg = f"不匹配: {'; '.join(mismatches)}"
        if non_device_items:
            skipped_names = {extract_short_product_name(item.product_category) for item in non_device_items}
            base_msg += f" (已跳过SaaS/运营服务/续保: {', '.join(sorted(skipped_names))})"
        return RuleResult(rule_id=6, rule_name="产品个数=配置项套数", result="不通过", message=base_msg)

    summary = ", ".join(f"{name}: {count}" for name, count in config_count.items())
    msg = f"各类型匹配: {summary or '无新购交付项'}"
    if non_device_items:
        skipped_names = {extract_short_product_name(item.product_category) for item in non_device_items}
        msg += f" (已跳过SaaS/运营服务/续保: {', '.join(sorted(skipped_names))})"
    return RuleResult(rule_id=6, rule_name="产品个数=配置项套数", result="通过", message=msg)


def _is_renewal_item(product_category: str) -> bool:
    parts = product_category.split("-")
    form_name = parts[-1] if parts else ""
    return "续保" in form_name