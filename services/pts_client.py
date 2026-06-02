"""PTS GraphQL client using Bearer token authentication.

Reuses the authentication pattern from delivery-aftersale-review:
- Endpoint: PTS GraphQL API
- Auth: Authorization: Bearer pt_xxx
- Rate limit: requests spaced ≥250ms apart (≤4 req/s)
- Variables must be inlined into query string (PTS API token mode doesn't support $variable parameters)

Mutations for work order closure:
- add_work_order_info: upload attachments to a work order
- confirm_work_order_stage: advance work order to next stage
"""

import asyncio
import logging
import re
import time

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# Rate limiting
_rate_limit_interval = 1.0 / _settings.pts_rate_limit  # seconds between requests
_last_call_time: float = 0.0
_rate_lock = asyncio.Lock()


async def _rate_limit() -> None:
    global _last_call_time
    async with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_call_time
        if elapsed < _rate_limit_interval:
            await asyncio.sleep(_rate_limit_interval - elapsed)
        _last_call_time = time.monotonic()


def _inline_variables(query: str, variables: dict | None = None) -> str:
    """Inline GraphQL variables into the query string.

    PTS API token mode doesn't support $variable parameterized queries.
    Converts: query Foo($id: ID!) { bar(id: $id) } → query Foo { bar(id: "xxx") }
    """
    if not variables:
        return query

    result = query

    # Remove variable declarations from operation definition
    result = re.sub(r"\((\$\w+:\s*\w+!?\s*,?\s*)+\)", "", result)

    # Replace variable references with literal values (sort by length to avoid partial replacements)
    entries = sorted(variables.items(), key=lambda x: len(x[0]), reverse=True)
    for key, value in entries:
        var_ref = f"${key}"
        if isinstance(value, str):
            literal = f'"{value}"'
        elif isinstance(value, bool):
            literal = str(value)
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = f'"{value}"'
        result = re.sub(re.escape(var_ref) + r"(?![\w])", literal, result)

    return result


async def pts_graphql_query(query: str, variables: dict | None = None) -> dict:
    """Send a GraphQL query to PTS API with Bearer token auth."""
    await _rate_limit()
    inlined_query = _inline_variables(query, variables)

    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            settings.pts_graphql_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.pts_api_token}",
            },
            json={"query": inlined_query},
        )

    if resp.status_code == 401:
        raise PermissionError("PTS API 令牌无效或已过期")
    if resp.status_code == 429:
        raise RuntimeError("PTS API 请求过于频繁，请稍后再试")
    if resp.status_code != 200:
        raise RuntimeError(f"PTS API 返回 HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if data.get("errors"):
        error_msg = data["errors"][0].get("message", str(data["errors"]))
        raise RuntimeError(f"GraphQL 错误: {error_msg}")

    return data.get("data", {})


async def verify_pts_token() -> bool:
    """Verify the PTS API token is valid."""
    try:
        await pts_graphql_query("{ me { id } }")
        return True
    except PermissionError:
        logger.warning("PTS API 令牌无效或已过期")
        return False
    except Exception as e:
        logger.warning("PTS API 令牌验证异常: %s", e)
        return False


async def introspect_schema() -> dict:
    """Run GraphQL introspection to discover the PTS schema."""
    query = """
    {
      __schema {
        queryType { name }
        mutationType { name }
        types {
          name
          kind
          fields {
            name
            type { name kind ofType { name kind } }
            args { name type { name kind ofType { name kind } } }
          }
        }
      }
    }
    """
    return await pts_graphql_query(query)


async def discover_work_order_types() -> list[dict]:
    """Discover work order related types from PTS schema."""
    schema = await introspect_schema()
    types = schema.get("__schema", {}).get("types", [])
    work_order_types = []
    keywords = ["work_order", "workorder", "inspection", "巡检"]
    for t in types:
        name = (t.get("name") or "").lower()
        if any(kw in name for kw in keywords):
            work_order_types.append(t)
    return work_order_types


async def query_inspection_work_orders(sync_month: str) -> list[dict]:
    """Query inspection work orders from PTS for the given month.

    Filters (applied locally since PTS search doesn't support plan_complete_date filter):
    - type includes: expert_service__product_inspection (产品巡检) or expert_service__log_analysis (日志分析)
    - plan_complete_date: within the given month (YYYY-MM)
    - is_finished: false (未闭环 only)
    - delivery.after_sale.name == "冯伟" (售后负责人为冯伟)
    """
    all_items = []
    skip = 0
    limit = 50

    while True:
        query = """
        {
          listWorkOrder(
            search: { type: [expert_service__product_inspection, expert_service__log_analysis] }
            pagination: { skip: %%SKIP%%, limit: %%LIMIT%% }
            sort: { sort: 1, by: "plan_complete_date" }
          ) {
            total
            data {
              id
              type
              is_finished
              company { id name claim_by { id name } }
              desc
              claim_by { id name username }
              plan_complete_date
              created_at
              related_product_info_type
              current_stage { name sequence }
              delivery {
                id
                project { id name }
                after_sale { id name username }
                assigner { id name username }
                contact_list {
                  contact { id name phone email }
                }
                product_info {
                  product_detail {
                    product { id name }
                  }
                }
              }
            }
          }
        }
        """.replace("%%SKIP%%", str(skip)).replace("%%LIMIT%%", str(limit))

        result = await pts_graphql_query(query)
        conn = result.get("listWorkOrder", {})
        total = conn.get("total", 0)
        items = conn.get("data", [])

        if not items:
            break

        all_items.extend(items)
        skip += limit

        if skip >= total:
            break

    # Local filter: plan_complete_date in the given month (UTC+8) AND not finished AND after_sale is 冯伟 AND not in completion stage
    from datetime import datetime, timedelta
    from services.aitable_fields import COMPLETION_STAGES

    filtered = []
    for item in all_items:
        plan = item.get("plan_complete_date", "")
        if not plan:
            continue
        # PTS returns UTC time; convert to China time (UTC+8) for month check
        try:
            utc_dt = datetime.fromisoformat(plan.replace("Z", "+00:00"))
            cn_dt = utc_dt + timedelta(hours=8)
            cn_month = cn_dt.strftime("%Y-%m")
        except (ValueError, TypeError):
            continue
        if cn_month != sync_month:
            continue
        if item.get("is_finished"):
            continue

        # Filter: exclude work orders in completion stage (审核工单, 已闭环)
        current_stage = item.get("current_stage", {})
        stage_name = current_stage.get("name", "") if isinstance(current_stage, dict) else ""
        if stage_name in COMPLETION_STAGES:
            continue

        # Filter: delivery.after_sale.name must be 冯伟
        delivery = item.get("delivery")
        after_sale = delivery.get("after_sale") if isinstance(delivery, dict) else None
        if not (isinstance(after_sale, dict) and after_sale.get("name") == "冯伟"):
            continue
        filtered.append(item)

    logger.info(
        "PTS fetched %d work orders, filtered to %d unclosed + plan_complete_date in %s + after_sale=冯伟 + not in completion stage",
        len(all_items), len(filtered), sync_month,
    )
    return filtered


async def add_work_order_info(work_order_id: str, note: str = "") -> bool:
    """Add note to a PTS work order.

    PTS add_work_order_info mutation returns Boolean.
    For file attachments, use upload_file() first, then embed markdown links in the note.
    """
    mutation = """
    mutation {
      add_work_order_info(id: "%s", note: "%s")
    }
    """ % (work_order_id, note.replace('"', '\\"').replace('\n', '\\n'))

    result = await pts_graphql_query(mutation)
    return result.get("add_work_order_info", False)


async def upload_file(file_path: str, filename: str | None = None) -> str | None:
    """Upload a file to PTS file storage via browser automation.

    PTS's /api/upload endpoint requires the web session cookie, which cannot
    be used from Python httpx directly (PTS reverse proxy rejects it).
    Instead, we use Playwright to upload the file from within the PTS page context,
    which automatically includes the session cookie.

    Returns the PTS file ID (e.g. "6a0ea81319ab1b9837973a00") on success,
    which can be used to construct download links as [/f/{file_id}].
    Returns None on failure.
    """
    import os

    if not filename:
        filename = os.path.basename(file_path)

    try:
        # Read file content and encode as base64 for transfer to browser
        import base64

        with open(file_path, "rb") as f:
            file_content = f.read()

        content_b64 = base64.b64encode(file_content).decode()

        # Use playwright to upload from within the PTS page context
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            # Set the session cookie
            settings = get_settings()
            cookie = settings.pts_session_cookie
            if not cookie:
                logger.warning("PTS session cookie not configured, cannot upload file")
                await browser.close()
                return None

            await context.add_cookies([{
                "name": "c",
                "value": cookie,
                "domain": "pts.chaitin.net",
                "path": "/",
            }])

            page = await context.new_page()

            # Navigate to PTS to establish the session
            await page.goto("https://pts.chaitin.net/project/delivery", wait_until="networkidle", timeout=30000)

            # Upload the file using fetch from within the page context
            js_code = """
            async (args) => {
                const [contentB64, fname] = args;
                const binaryStr = atob(contentB64);
                const bytes = new Uint8Array(binaryStr.length);
                for (let i = 0; i < binaryStr.length; i++) {
                    bytes[i] = binaryStr.charCodeAt(i);
                }
                const blob = new Blob([bytes]);
                const formData = new FormData();
                formData.append('file', blob, fname);

                const resp = await fetch('/api/upload', {
                    method: 'POST',
                    body: formData,
                });
                return await resp.json();
            }
            """

            result = await page.evaluate(js_code, [content_b64, filename])

            await browser.close()

        if result.get("err") != 0:
            logger.error("PTS file upload error: %s", result)
            return None

        file_id = result.get("id")
        logger.info("Uploaded %s to PTS: id=%s, filename=%s", filename, file_id, result.get("filename"))
        return file_id

    except Exception as e:
        logger.error("PTS file upload exception: %s", e)
        return None


async def query_work_order_status(pts_order_id: str) -> dict | None:
    """Query a single PTS work order's closure status by ID.

    Returns dict with keys: id, is_finished, current_stage { name sequence }
    """
    query = """
    {
      workOrderByID(id: "%s") {
        id
        is_finished
        current_stage { name sequence }
      }
    }
    """ % pts_order_id
    result = await pts_graphql_query(query)
    return result.get("workOrderByID")


async def confirm_work_order_stage(work_order_id: str) -> bool | None:
    """Advance a PTS work order to the next stage.

    PTS confirm_work_order_stage mutation returns Boolean (true on success, null on failure).
    May need to be called multiple times to advance through all stages to finished.
    """
    mutation = """
    mutation {
      confirm_work_order_stage(id: "%s")
    }
    """ % work_order_id

    result = await pts_graphql_query(mutation)
    return result.get("confirm_work_order_stage")


async def update_work_order_plan_complete_date(work_order_id: str, plan_complete_date_utc: str) -> bool:
    """Update a PTS work order's plan_complete_date via mutation.

    Args:
        work_order_id: PTS work order ID
        plan_complete_date_utc: UTC datetime string, e.g. "2026-06-29T16:00:00Z"
            (represents 2026-06-30 00:00 Beijing time)

    Returns:
        True on success, False on failure.
    """
    mutation = """
    mutation {
      update_work_order(id: "%s", input: { plan_complete_date: "%s" })
    }
    """ % (work_order_id, plan_complete_date_utc)

    try:
        result = await pts_graphql_query(mutation)
        # update_work_order returns Boolean (null/true on success, errors on failure)
        if result.get("errors"):
            logger.error("PTS update_work_order failed for %s: %s", work_order_id, result["errors"])
            return False
        return True
    except Exception as e:
        logger.error("PTS update_work_order exception for %s: %s", work_order_id, e)
        return False
