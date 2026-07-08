"""规则 3：交付项及配置项不能为空"""

from services.review.audit.schemas import DeliveryItem, RuleResult
from services.review.audit.product_type import is_operation_service_item


def rule3_delivery_items(delivery_items: list[DeliveryItem]) -> RuleResult:
    if not delivery_items:
        return RuleResult(
            rule_id=3, rule_name="交付项及配置项不为空", result="不通过",
            message="交付项表格无数据行",
        )

    # 运营服务类交付项（如"洞鉴-产品运营服务"）没有配置项是正常的，不纳入检查
    checkable_items = [item for item in delivery_items if not is_operation_service_item(item.product_category)]
    skipped = len(delivery_items) - len(checkable_items)

    empty_items = [
        item for item in checkable_items
        if not item.config_items or all(not ci.text.strip() for ci in item.config_items)
    ]

    if empty_items:
        return RuleResult(
            rule_id=3, rule_name="交付项及配置项不为空", result="不通过",
            message=f"{len(empty_items)} 个交付项的配置项为空（共 {len(delivery_items)} 个交付项）",
        )

    total_ci = sum(len(item.config_items) for item in checkable_items)
    suffix = f"，{skipped} 个运营服务项豁免" if skipped > 0 else ""
    return RuleResult(
        rule_id=3, rule_name="交付项及配置项不为空", result="通过",
        message=f"{len(delivery_items)} 个交付项，{total_ci} 个配置项{suffix}",
    )