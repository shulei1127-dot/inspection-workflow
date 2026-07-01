"""PTS 回访工单 GraphQL 客户端

基于 closed_loop_v2 反编译的 VisitRealRunner，实现回访工单的完整生命周期：
  1. fetch_delivery_metadata  — 查询交付元数据（company_id, contact_id, product_id, form_id）
  2. create_visit             — 创建回访工单
  3. find_created_visit       — 定位刚创建的回访（获取 visit_id）
  4. fetch_visit_detail       — 查询回访详情（获取 content_id）
  5. process_visit            — 填写反馈（满意度 + 备注）
  6. finish_visit             — 完成回访

复用 review/pts_review_client.py 的 review_pts_query() 和 enum_value() 机制。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from services.review.pts_review_client import enum_value, review_pts_query

logger = logging.getLogger(__name__)

# ── 回访类型映射 ────────────────────────────────────────────────

PTS_VISIT_TYPE_MAP: dict[str, str] = {
    "return_visit": "return_visit",
    "回访": "return_visit",
    "visit": "return_visit",
}

# ── 回访链接模板 ────────────────────────────────────────────────

VISIT_URL_TEMPLATE = "https://pts.chaitin.net/return-visit/detail/{visit_id}"

# ── GraphQL 查询常量 ────────────────────────────────────────────

QUERY_DELIVERY_METADATA = """query DeliveryMetadata($id: ID!) {
  list_product_delivery(
    search: { id: $id },
    pagination: { skip: 0, limit: 10 },
    SortBy: { by: "updated_at", sort: -1 }
  ) {
    total
    data {
      id
      project {
        id
        name
        company {
          id
          name
          contact {
            id
            name
            area_code
            phone
            email
            duty
          }
        }
        product_detail_list {
          product { id name }
          form { id name }
        }
      }
      visit_data {
        visit_finished
      }
    }
  }
}"""

MUTATION_CREATE_VISIT = """mutation CreateVisit($input: VisitInput!) {
  create_visit(input: $input)
}"""

QUERY_LIST_VISITS = """query ListVisits($company_id: String!, $delivery_id: String!, $visitor_ids: [String!]) {
  list_visit(
    search: {
      company_id: $company_id,
      delivery_id: $delivery_id,
      visitor_ids: $visitor_ids,
      finished: false
    },
    pagination: { skip: 0, limit: 20 }
  ) {
    total
    data {
      id
      type
      finished
      created_at
      company { id name }
      visitor { id name }
    }
  }
}"""

QUERY_VISIT_DETAIL = """query VisitDetail($id: ID!) {
  visit_detail(id: $id) {
    id
    type
    finished
    company { id name }
    visitor { id name }
    contact_list {
      contact { id name area_code phone email duty }
      visit_object
      note
    }
    content_list {
      id
      score
      feedback_note
      product_detail { product { id name } form { id name } }
      delivery_list {
        delivery_id
        delivery_type
        project { id name company { id name } }
      }
    }
  }
}"""

MUTATION_PROCESS_VISIT = """mutation ProcessVisit($id: ID!, $contact_list: [VisitContactInput!], $way: VisitWay!, $visit_time: String!, $content_list: [VisitContentInput!]) {
  process_visit(
    id: $id,
    contact_list: $contact_list,
    way: $way,
    visit_time: $visit_time,
    content_list: $content_list
  )
}"""

MUTATION_FINISH_VISIT = """mutation FinishVisit($id: ID!) {
  finish_visit(id: $id)
}"""


# ── 便捷查询函数 ────────────────────────────────────────────────

async def fetch_delivery_metadata(delivery_id: str) -> dict[str, Any] | None:
    """查询交付元数据，提取 company_id, contact_id, product_id, form_id 等。

    Args:
        delivery_id: PTS 交付 ID（即 project_id / product_delivery 的 id）

    Returns:
        解析后的元数据字典，失败返回 None
    """
    try:
        data = await review_pts_query(QUERY_DELIVERY_METADATA, {"id": delivery_id})
        delivery_list = data.get("list_product_delivery", {}).get("data", [])
        if not delivery_list:
            logger.warning("交付元数据为空: delivery_id=%s", delivery_id)
            return None

        delivery = delivery_list[0]
        project = delivery.get("project", {})
        company = project.get("company", {})
        contacts = company.get("contact", [])
        product_detail_list = project.get("product_detail_list", [])

        # 选择联系人：优先选有手机号的
        contact = None
        for c in contacts:
            if c.get("phone"):
                contact = c
                break
        if not contact and contacts:
            contact = contacts[0]

        # 选择产品详情：优先选有 form_id 的
        product_detail = None
        for pd in product_detail_list:
            if pd.get("form", {}).get("id"):
                product_detail = pd
                break
        if not product_detail and product_detail_list:
            product_detail = product_detail_list[0]

        result = {
            "delivery_id": delivery.get("id"),
            "project_id": project.get("id"),
            "project_name": project.get("name"),
            "company_id": company.get("id"),
            "company_name": company.get("name"),
            "contact_id": contact.get("id") if contact else None,
            "contact_name": contact.get("name") if contact else None,
            "contact_phone": contact.get("phone") if contact else None,
            "product_id": product_detail.get("product", {}).get("id") if product_detail else None,
            "product_name": product_detail.get("product", {}).get("name") if product_detail else None,
            "form_id": product_detail.get("form", {}).get("id") if product_detail else None,
            "form_name": product_detail.get("form", {}).get("name") if product_detail else None,
            "visit_finished": delivery.get("visit_data", {}).get("visit_finished", False),
        }

        logger.info(
            "交付元数据: delivery_id=%s, company=%s, contact=%s, product=%s",
            delivery_id, result["company_id"], result["contact_id"], result["product_id"],
        )
        return result

    except Exception:
        logger.exception("查询交付元数据失败: delivery_id=%s", delivery_id)
        return None


async def create_visit(
    *,
    company_id: str,
    visitor_id: str,
    visit_type: str = "return_visit",
    contact_id: str | None = None,
    product_id: str | None = None,
    form_id: str | None = None,
    delivery_id: str | None = None,
) -> dict[str, Any] | None:
    """创建回访工单。

    Args:
        company_id: 公司 ID（同时作为 visitor_id）
        visitor_id: 回访者 ID（通常等于 company_id）
        visit_type: 回访类型（默认 return_visit）
        contact_id: 联系人 ID
        product_id: 产品 ID
        form_id: 表单 ID
        delivery_id: 交付 ID

    Returns:
        GraphQL mutation 返回值，失败返回 None
    """
    # 构建 contact_list
    contact_list = []
    if contact_id:
        contact_list.append({
            "contact_id": contact_id,
            "visit_object": True,
            "note": "",
        })

    # 构建 content_list
    content_list = []
    if product_id and form_id and delivery_id:
        content_list.append({
            "product_detail": {
                "product": product_id,
                "form": form_id,
            },
            "delivery_list": [
                {
                    "delivery_id": delivery_id,
                    "delivery_type": enum_value("product_delivery"),
                }
            ],
        })

    # 映射回访类型
    mapped_type = PTS_VISIT_TYPE_MAP.get(visit_type, "return_visit")

    # 使用标准 GraphQL variables（与 submit_review 同理，避免 inline 时特殊字符问题）
    settings = get_settings()
    token = settings.pts_visit_api_token or settings.pts_review_api_token or settings.pts_api_token

    import httpx
    from services.pts_client import _rate_limit

    variables = {
        "input": {
            "company_id": company_id,
            "visitor_id": visitor_id,
            "type": mapped_type,
            "contact_list": contact_list,
            "content_list": content_list,
        },
    }

    await _rate_limit()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.pts_graphql_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={"query": MUTATION_CREATE_VISIT, "variables": variables},
        )

    if resp.status_code == 401:
        raise PermissionError("PTS API 令牌无效或已过期")
    if resp.status_code != 200:
        logger.error("创建回访工单失败: HTTP %d, body=%s", resp.status_code, resp.text[:300])
        return None

    data = resp.json()
    if data.get("errors"):
        error_msg = data["errors"][0].get("message", str(data["errors"]))
        logger.error("创建回访工单 GraphQL 错误: %s", error_msg)
        return None

    result = data.get("data", {})
    logger.info("创建回访工单成功: company_id=%s, delivery_id=%s", company_id, delivery_id)
    return result


async def find_created_visit(
    *,
    company_id: str,
    delivery_id: str,
    visitor_id: str,
) -> str | None:
    """查找已创建的回访工单，返回 visit_id。

    create_visit mutation 不直接返回 visit_id，需要通过 list_visit 查询定位。

    Args:
        company_id: 公司 ID
        delivery_id: 交付 ID
        visitor_id: 回访者 ID

    Returns:
        visit_id 字符串，未找到返回 None
    """
    try:
        data = await review_pts_query(
            QUERY_LIST_VISITS,
            {
                "company_id": company_id,
                "delivery_id": delivery_id,
                "visitor_ids": [visitor_id],
            },
        )

        visits = data.get("list_visit", {}).get("data", [])
        if not visits:
            logger.warning("未找到回访工单: company_id=%s, delivery_id=%s", company_id, delivery_id)
            return None

        # 取最新的未完成回访
        visit = visits[0]
        visit_id = visit.get("id")
        logger.info("找到回访工单: visit_id=%s, type=%s", visit_id, visit.get("type"))
        return visit_id

    except Exception:
        logger.exception("查找回访工单失败: company_id=%s, delivery_id=%s", company_id, delivery_id)
        return None


async def fetch_visit_detail(visit_id: str) -> dict[str, Any] | None:
    """查询回访工单详情，获取 content_id 等关键数据。

    Args:
        visit_id: 回访工单 ID

    Returns:
        解析后的详情字典，失败返回 None
    """
    try:
        data = await review_pts_query(QUERY_VISIT_DETAIL, {"id": visit_id})
        detail = data.get("visit_detail")
        if not detail:
            logger.warning("回访详情为空: visit_id=%s", visit_id)
            return None

        content_list = detail.get("content_list", [])
        contact_list = detail.get("contact_list", [])

        result = {
            "visit_id": detail.get("id"),
            "type": detail.get("type"),
            "finished": detail.get("finished", False),
            "content_id": content_list[0].get("id") if content_list else None,
            "contact_id": contact_list[0].get("contact", {}).get("id") if contact_list else None,
            "content_list": content_list,
            "contact_list": contact_list,
        }

        logger.info("回访详情: visit_id=%s, content_id=%s, finished=%s", visit_id, result["content_id"], result["finished"])
        return result

    except Exception:
        logger.exception("查询回访详情失败: visit_id=%s", visit_id)
        return None


async def process_visit(
    *,
    visit_id: str,
    contact_id: str | None = None,
    content_id: str | None = None,
    score: int = 5,
    note: str = "自动回访完成",
) -> dict[str, Any] | None:
    """填写回访反馈（满意度 + 备注）。

    Args:
        visit_id: 回访工单 ID
        contact_id: 联系人 ID
        content_id: 回访内容 ID（从 visit_detail 获取）
        score: 满意度评分 (1-5)
        note: 反馈备注

    Returns:
        GraphQL mutation 返回值，失败返回 None
    """
    settings = get_settings()
    token = settings.pts_visit_api_token or settings.pts_review_api_token or settings.pts_api_token

    import httpx
    from services.pts_client import _rate_limit

    # 构建 contact_list
    contact_list = []
    if contact_id:
        contact_list.append({
            "contact_id": contact_id,
            "visit_object": True,
            "note": "",
        })

    # 构建 content_list
    content_list = []
    if content_id:
        content_list.append({
            "id": content_id,
            "score": score,
            "feedback_note": note,
        })

    # 当前 UTC ISO 时间戳
    visit_time = datetime.now(timezone.utc).isoformat()

    variables = {
        "id": visit_id,
        "contact_list": contact_list,
        "way": "phone",
        "visit_time": visit_time,
        "content_list": content_list,
    }

    await _rate_limit()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.pts_graphql_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={"query": MUTATION_PROCESS_VISIT, "variables": variables},
        )

    if resp.status_code == 401:
        raise PermissionError("PTS API 令牌无效或已过期")
    if resp.status_code != 200:
        logger.error("填写回访反馈失败: HTTP %d, body=%s", resp.status_code, resp.text[:300])
        return None

    data = resp.json()
    if data.get("errors"):
        error_msg = data["errors"][0].get("message", str(data["errors"]))
        logger.error("填写回访反馈 GraphQL 错误: %s", error_msg)
        return None

    result = data.get("data", {})
    logger.info("填写回访反馈成功: visit_id=%s, score=%d", visit_id, score)
    return result


async def finish_visit(visit_id: str) -> dict[str, Any] | None:
    """完成回访工单。

    Args:
        visit_id: 回访工单 ID

    Returns:
        GraphQL mutation 返回值，失败返回 None
    """
    settings = get_settings()
    token = settings.pts_visit_api_token or settings.pts_review_api_token or settings.pts_api_token

    import httpx
    from services.pts_client import _rate_limit

    await _rate_limit()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.pts_graphql_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={"query": MUTATION_FINISH_VISIT, "variables": {"id": visit_id}},
        )

    if resp.status_code == 401:
        raise PermissionError("PTS API 令牌无效或已过期")
    if resp.status_code != 200:
        logger.error("完成回访失败: HTTP %d, body=%s", resp.status_code, resp.text[:300])
        return None

    data = resp.json()
    if data.get("errors"):
        error_msg = data["errors"][0].get("message", str(data["errors"]))
        logger.error("完成回访 GraphQL 错误: %s", error_msg)
        return None

    result = data.get("data", {})
    logger.info("完成回访成功: visit_id=%s", visit_id)
    return result
