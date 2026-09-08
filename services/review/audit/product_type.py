"""产品类型识别 — 关键词匹配"""

from __future__ import annotations

from services.review.audit.schemas import ProductInfo, ProductType

SAAS_KEYWORDS = [
    "云图", "百川云", "大观", "无锋", "MSS标准版",
    "互联网暴露面检测评估服务", "虚拟坐席重保版", "产品运营服务",
    "智能编程助手", "monkeycode",
]
HARDWARE_KEYWORDS = ["硬件版", "硬件租用版"]
SOFTWARE_KEYWORDS = ["软件版", "软件订阅版"]

KEY_PRODUCT_KEYWORDS = ["雷池", "洞鉴", "牧云", "谛听", "全悉", "万象"]
MAINSTREAM_PRODUCT_KEYWORDS = [
    "雷池", "洞鉴", "牧云", "万象", "全悉", "谛听",
    "长亭运维审计", "长亭流量分析预警", "长亭日志审计", "长亭网页防篡改",
    "长亭第二代防火墙", "长亭漏洞管理", "长亭上网行为审计",
    "长亭数据库审计", "长亭安全认证网关", "入侵检测防御",
]
# 产品形态名中含这些关键词的交付项是运营/服务类，不是产品设备，没有配置项和产品实例是正常的
OPERATION_SERVICE_FORM_KEYWORDS = ["产品运营服务", "运营服务"]


def identify_product_type(category: str, summary: str) -> ProductType:
    text = f"{category} {summary}"
    for kw in SAAS_KEYWORDS:
        if kw in text:
            return ProductType.saas
    for kw in HARDWARE_KEYWORDS:
        if kw in text:
            return ProductType.hardware
    for kw in SOFTWARE_KEYWORDS:
        if kw in text:
            return ProductType.software
    return ProductType.hardware


def is_mainstream_product(product: ProductInfo) -> bool:
    text = product.product_category or product.summary or ""
    return any(kw in text for kw in MAINSTREAM_PRODUCT_KEYWORDS)


def is_key_product(product: ProductInfo) -> bool:
    text = f"{product.summary or ''} {product.product_category or ''}"
    return any(kw in text for kw in KEY_PRODUCT_KEYWORDS)


def get_key_product_name(product: ProductInfo) -> str:
    text = product.summary or product.product_category or ""
    for name in KEY_PRODUCT_KEYWORDS:
        if name in text:
            return name
    return ""


def extract_short_product_name(product_category: str) -> str:
    for kw in KEY_PRODUCT_KEYWORDS:
        if kw in product_category:
            return kw
    dash_idx = product_category.find("-")
    return product_category[:dash_idx] if dash_idx > 0 else product_category


def is_operation_service_item(product_category: str) -> bool:
    """判断交付项是否为运营服务类（如"洞鉴-产品运营服务"），这类交付项没有配置项和产品实例是正常的"""
    parts = product_category.split("-")
    form_name = parts[-1] if parts else ""
    return any(kw in form_name for kw in OPERATION_SERVICE_FORM_KEYWORDS)
