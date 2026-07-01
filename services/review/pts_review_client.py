"""PTS 审核专用查询层

基于 services/pts_client.py 的 pts_graphql_query() 扩展，增加：
- 审核专用 GraphQL 查询常量
- enum_value() 辅助函数（inline_variables 需要）
- 429 重试逻辑
- 审核专用查询和 mutation 函数
"""

from __future__ import annotations

import logging
from typing import Any

from services.pts_client import _rate_limit
from core.config import get_settings

logger = logging.getLogger(__name__)


# ── enum_value 辅助 ──────────────────────────────────────────

class _EnumMarker:
    """Wrapper so inline_variables emits the value without quotes (GraphQL enum)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


def enum_value(value: str) -> _EnumMarker:
    """Mark a value as a GraphQL enum so inline_variables omits quotes."""
    return _EnumMarker(value)


def _inline_list(items: list[Any]) -> str:
    """Convert a Python list to a GraphQL list literal."""
    elements: list[str] = []
    for item in items:
        if isinstance(item, _EnumMarker):
            elements.append(item.value)
        elif isinstance(item, bool):
            elements.append(str(item).lower())
        elif isinstance(item, (int, float)):
            elements.append(str(item))
        elif isinstance(item, str):
            elements.append(f'"{item}"')
        else:
            elements.append(f'"{item}"')
    return "[" + ", ".join(elements) + "]"


def _inline_variables_enhanced(query: str, variables: dict | None = None) -> str:
    """增强版 inline_variables，支持 enum_value 和列表类型。"""
    if not variables:
        return query

    import re

    result = query

    # Remove variable declarations from operation definition
    result = re.sub(r"\((\$\w+:\s*\w+!?\s*,?\s*)+\)", "", result)

    # Replace variable references with literal values (sort by length to avoid partial replacements)
    entries = sorted(variables.items(), key=lambda x: len(x[0]), reverse=True)
    for key, value in entries:
        var_ref = f"${key}"
        if isinstance(value, _EnumMarker):
            literal = value.value
        elif isinstance(value, list):
            literal = _inline_list(value)
        elif isinstance(value, bool):
            literal = str(value).lower()
        elif isinstance(value, (int, float)):
            literal = str(value)
        elif isinstance(value, str):
            literal = f'"{value}"'
        else:
            literal = f'"{value}"'
        result = re.sub(re.escape(var_ref) + r"(?![\w])", literal, result)

    return result


# ── GraphQL 查询常量 ──────────────────────────────────────────

QUERY_DELIVERY_BY_ID = """query DeliveryById($id: ID!) {
  product_delivery_by_id(id: $id) {
    id
    project {
      id
      name
      delivery_type
      company {
        id
        name
        contact {
          id
          name
          phone
          email
          duty
          __typename
        }
        __typename
      }
      __typename
    }
    delivery_status
    product_delivery_project {
      products {
        product {
          id
          name
          group
          __typename
        }
        form {
          id
          name
          __typename
        }
        infos {
          version {
            name
            module_groups {
              name
              modules {
                name
                number
                __typename
              }
              __typename
            }
            __typename
          }
          number
          __typename
        }
        __typename
      }
      __typename
    }
    item_list {
      id
      product_detail {
        product {
          id
          name
          group
          __typename
        }
        form {
          id
          name
          __typename
        }
        __typename
      }
      end_at
      over_guarantee
      work_hour
      valid
      __typename
    }
    after_sale {
      id
      name
      username
      __typename
    }
    assigner {
      id
      name
      username
      __typename
    }
    person_in_charge {
      id
      name
      username
      __typename
    }
    product_info {
      id
      type
      product_detail {
        product {
          id
          name
          __typename
        }
        form {
          id
          name
          __typename
        }
        __typename
      }
      delivery {
        id
        project {
          id
          name
          company {
            id
            name
            __typename
          }
          __typename
        }
        __typename
      }
      desc
      number
      __typename
    }
    contact_list {
      return_visit
      contact {
        id
        name
        phone
        email
        duty
        __typename
      }
      __typename
    }
    __typename
  }
}"""

QUERY_PRODUCT_INFO_BY_ID = """query ProductInfoByID($id: ID!) {
  productInfoByID(id: $id) {
    id
    type
    desc
    number
    over_guarantee
    product_detail {
      product {
        id
        name
        __typename
      }
      form {
        id
        name
        __typename
      }
      __typename
    }
    after_info {
      type_number
      serial_number
      machine_code
      stage_mode
      product_version
      engine_version
      is_ha
      license_nature
      license_validity
      license_id
      after_sales_validity
      rank
      desc
      note
      needle_version
      needle_number
      patch_version
      __typename
    }
    __typename
  }
}"""

QUERY_PENDING_DELIVERY_LIST = """{
  list_product_delivery(
    search: { delivery_status: to_after_sale_review, after_sale: $after_sale_ids },
    pagination: { skip: 0, limit: 100 },
    SortBy: { by: "id", sort: 1 }
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
          __typename
        }
        __typename
      }
      delivery_status
      after_sale {
        id
        name
        username
        __typename
      }
      assigner {
        name
        username
        __typename
      }
      person_in_charge {
        name
        username
        __typename
      }
      __typename
    }
  }
}"""

QUERY_RELATED_DELIVERY_TASK_LIST = """query RelatedDeliveryTaskList($related_type: Target!, $related_id: String!) {
  related_delivery_task_list(related_type: $related_type, related_id: $related_id) {
    id
    index
    task_template
    task_type
    related_type
    status
    start_at
    end_at
    task_person {
      id
      name
      username
      __typename
    }
    __typename
  }
}"""

QUERY_DELIVERY_TASK_BY_ID = """query DeliveryTask($id: String!) {
  delivery_task(id: $id) {
    id
    task_type
    status
    finished_at
    task_person {
      id
      name
      username
      __typename
    }
    reviewer {
      id
      name
      username
      __typename
    }
    comment {
      id
      content
      create_type
      created_at
      creator {
        id
        name
        username
        __typename
      }
      __typename
    }
    __typename
  }
}"""

REVIEW_AFTER_SALE_MUTATION = """mutation ReviewProductDeliveryAfterSale($id: String!, $status: Boolean!, $reason: String!) {
  review_product_delivery_after_sale(id: $id, status: $status, reason: $reason)
}"""


# ── 带重试的审核查询 ───────────────────────────────────────────

_429_MAX_RETRIES = 3
_429_BASE_DELAY_S = 2.0


async def review_pts_query(query: str, variables: dict | None = None) -> dict:
    """带 429 重试的 PTS 查询，使用增强版 inline_variables 支持 enum_value。

    与 pts_graphql_query() 不同：
    - 支持 enum_value() 类型变量
    - 429 限流时自动重试（3次，指数退避）
    - 错误直接抛出（不吞异常），确保审批时间等关键数据不会误判
    """
    import asyncio
    import httpx

    settings = get_settings()
    token = settings.pts_review_api_token or settings.pts_api_token

    inlined_query = _inline_variables_enhanced(query, variables)

    last_exc: Exception | None = None
    for attempt in range(1, _429_MAX_RETRIES + 1):
        await _rate_limit()

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                settings.pts_graphql_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                json={"query": inlined_query},
            )

        if resp.status_code == 401:
            raise PermissionError("PTS API 令牌无效或已过期")
        if resp.status_code == 429:
            delay = _429_BASE_DELAY_S * attempt
            if attempt < _429_MAX_RETRIES:
                logger.warning("PTS API 429 限流，%.0fs 后重试 (%d/%d)", delay, attempt, _429_MAX_RETRIES)
                await asyncio.sleep(delay)
                last_exc = RuntimeError(f"PTS API 429 限流，重试 {attempt}/{_429_MAX_RETRIES}")
                continue
            else:
                raise RuntimeError(f"PTS API 持续 429 限流，已重试 {_429_MAX_RETRIES} 次")
        if resp.status_code != 200:
            raise RuntimeError(f"PTS API 返回 HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if data.get("errors"):
            error_msg = data["errors"][0].get("message", str(data["errors"]))
            raise RuntimeError(f"GraphQL 错误: {error_msg}")

        return data.get("data", {})

    # Should not reach here, but just in case
    raise last_exc or RuntimeError("PTS API 查询失败")


# ── 便捷查询函数 ──────────────────────────────────────────────

async def fetch_pending_list(*, after_sale_ids: list[str] | None = None) -> dict | None:
    """获取待审核项目列表。"""
    if after_sale_ids:
        variables = {"after_sale_ids": after_sale_ids}
    else:
        # 移除 after_sale 过滤条件，查询所有 to_after_sale_review 项目
        query = QUERY_PENDING_DELIVERY_LIST.replace(", after_sale: $after_sale_ids", "")
        return await review_pts_query(query)

    return await review_pts_query(QUERY_PENDING_DELIVERY_LIST, variables)


async def fetch_delivery_by_id(project_id: str) -> dict | None:
    """按 ID 获取完整交付/项目数据。"""
    try:
        return await review_pts_query(QUERY_DELIVERY_BY_ID, {"id": project_id})
    except Exception:
        logger.exception("fetch_delivery_by_id failed: project_id=%s", project_id)
        return None


async def fetch_product_info_by_id(product_id: str) -> dict | None:
    """按 ID 获取产品详情。"""
    try:
        return await review_pts_query(QUERY_PRODUCT_INFO_BY_ID, {"id": product_id})
    except Exception:
        logger.exception("fetch_product_info_by_id failed: product_id=%s", product_id)
        return None


async def fetch_related_tasks(project_id: str) -> dict | None:
    """获取与项目关联的交付任务列表。"""
    return await review_pts_query(
        QUERY_RELATED_DELIVERY_TASK_LIST,
        {"related_type": enum_value("product_delivery"), "related_id": project_id},
    )


async def fetch_task_detail(task_id: str) -> dict | None:
    """按 ID 获取交付任务详情。"""
    try:
        return await review_pts_query(QUERY_DELIVERY_TASK_BY_ID, {"id": task_id})
    except Exception:
        logger.exception("fetch_task_detail failed: task_id=%s", task_id)
        return None


async def submit_review(project_id: str, approved: bool, reason: str) -> dict | None:
    """调用 PTS mutation 提交审核结果。"""
    settings = get_settings()
    token = settings.pts_review_approval_api_key
    if not token:
        logger.warning("pts_review_approval_api_key 未配置，无法回写 PTS 审核结果")
        return None

    import httpx

    # 使用标准 GraphQL variables 传递参数，避免 inline variables 时
    # reason 中的换行符/双引号破坏查询语法
    payload = {
        "query": REVIEW_AFTER_SALE_MUTATION,
        "variables": {
            "id": project_id,
            "status": approved,
            "reason": reason,
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
            json=payload,
        )

    if resp.status_code != 200:
        logger.error(
            "PTS review mutation failed: HTTP %d, body=%s",
            resp.status_code, resp.text[:300],
        )
        return None

    data = resp.json()
    if data.get("errors"):
        logger.error("PTS review mutation error: %s", data["errors"])
        return None

    result = data.get("data", {})
    logger.info(
        "PTS review mutation success: project_id=%s, approved=%s",
        project_id, approved,
    )
    return result
