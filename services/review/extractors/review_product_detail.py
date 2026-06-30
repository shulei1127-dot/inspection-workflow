"""第 2 步：产品详情数据提取

通过 PTS GraphQL API 直接获取产品详情（含 Meta 字段：序列号、机器码、服务期等）。
"""

from __future__ import annotations

import logging

from services.review.audit.schemas import ProductInfo
from services.review.extractors.review_api_mapper import map_product_from_graphql
from services.review.pts_review_client import fetch_product_info_by_id

logger = logging.getLogger(__name__)


async def extract_product_detail(product_id: str) -> ProductInfo | None:
    """通过 GraphQL 查询获取产品详情并映射为 ProductInfo。

    Args:
        product_id: PTS 产品信息 ID

    Returns:
        ProductInfo 产品详情，查询失败时返回 None

    Raises:
        ValueError: 无法获取产品详情时
    """
    result = await fetch_product_info_by_id(product_id)

    if result and result.get("productInfoByID"):
        logger.info("GraphQL 提取产品详情成功: product_id=%s", product_id)
        return map_product_from_graphql(result["productInfoByID"])

    raise ValueError(f"无法获取产品详情：product_id={product_id}")
