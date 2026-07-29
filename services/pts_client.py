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
import hashlib
import logging
import re
import threading
import time

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# Rate limiting
_rate_limit_interval = 1.0 / _settings.pts_rate_limit  # seconds between requests
_last_call_time: float = 0.0
_rate_lock = threading.Lock()  # threading.Lock is safe across asyncio event loops


async def _rate_limit() -> None:
    """Enforce PTS API rate limit (4 req/s).

    Uses threading.Lock instead of asyncio.Lock because APScheduler runs
    sync jobs that call asyncio.run() in separate threads, creating new
    event loops. asyncio.Lock is bound to the event loop where it was
    created and raises RuntimeError when used from a different loop.
    threading.Lock has no such limitation.
    """
    global _last_call_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_call_time
        if elapsed < _rate_limit_interval:
            wait_time = _rate_limit_interval - elapsed
            _last_call_time = now + wait_time
        else:
            wait_time = 0
            _last_call_time = now

    if wait_time > 0:
        await asyncio.sleep(wait_time)


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


async def pts_graphql_query(query: str, variables: dict | None = None, max_retries: int = 3) -> dict:
    """Send a GraphQL query to PTS API with Bearer token auth.

    Retries up to max_retries times on 429 (rate limit) with exponential backoff.
    """
    inlined_query = _inline_variables(query, variables)
    settings = get_settings()

    for attempt in range(max_retries + 1):
        await _rate_limit()

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
            if attempt < max_retries:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                logger.warning("PTS API rate limited (429), retrying in %ds (attempt %d/%d)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
                continue
            raise RuntimeError("PTS API 请求过于频繁，请稍后再试")
        if resp.status_code != 200:
            raise RuntimeError(f"PTS API 返回 HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if data.get("errors"):
            error_msg = data["errors"][0].get("message", str(data["errors"]))
            raise RuntimeError(f"GraphQL 错误: {error_msg}")

        return data.get("data", {})

    raise RuntimeError("PTS API 请求过于频繁，请稍后再试")


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

    Optimization: sort DESCENDING by plan_complete_date so newest data comes first,
    and stop pagination once we've passed the target month (no more matches possible).
    """
    from datetime import datetime, timedelta
    from services.aitable_fields import COMPLETION_STAGES

    all_items = []
    skip = 0
    limit = 50
    past_target_month = False

    while True:
        query = """
        {
          listWorkOrder(
            search: { type: [expert_service__product_inspection, expert_service__log_analysis] }
            pagination: { skip: %%SKIP%%, limit: %%LIMIT%% }
            sort: { sort: -1, by: "plan_complete_date" }
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

        # Check if all items in this page are before the target month (descending order)
        # If so, no more matching data can be found in subsequent pages
        for item in items:
            plan = item.get("plan_complete_date", "")
            if not plan:
                all_items.append(item)
                continue
            try:
                utc_dt = datetime.fromisoformat(plan.replace("Z", "+00:00"))
                cn_month = (utc_dt + timedelta(hours=8)).strftime("%Y-%m")
                if cn_month < sync_month:
                    past_target_month = True
                    break
            except (ValueError, TypeError):
                pass
            all_items.append(item)

        if past_target_month:
            break

        skip += limit
        if skip >= total:
            break

    # Local filter: plan_complete_date in the given month (UTC+8) AND not finished AND after_sale is 冯伟 AND not in completion stage
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


async def add_work_order_info(
    work_order_id: str,
    note: str = "",
    file_ids: list[str] | None = None,
) -> bool:
    """Add note and/or file attachments to a PTS work order.

    Args:
        work_order_id: PTS work order ID.
        note: Text note to add (special chars auto-escaped).
        file_ids: List of PTS file IDs (from upload_file_via_api) to attach.

    Returns:
        True on success, False on failure.
    """
    escaped_note = note.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    file_part = ""
    if file_ids:
        ids_str = ", ".join(f'"{fid}"' for fid in file_ids)
        file_part = f", file: [{ids_str}]"

    mutation = """
    mutation {
      add_work_order_info(id: "%s", note: "%s"%s)
    }
    """ % (work_order_id, escaped_note, file_part)

    result = await pts_graphql_query(mutation)
    return result.get("add_work_order_info", False)


async def upload_file_via_api(file_content: bytes, filename: str) -> str | None:
    """Upload a file to PTS via internal API (Bearer token auth).

    Uses the PTS /api/upload endpoint with cat=work_order.
    This replaces the Playwright-based upload_file() method.

    Args:
        file_content: Raw file bytes.
        filename: Original filename (e.g. "巡检报告.pdf").

    Returns:
        PTS file ID (e.g. "6a0ea81319ab1b9837973a00") on success, None on failure.
    """
    settings = get_settings()
    upload_url = settings.pts_upload_url

    if not upload_url:
        logger.error("PTS upload URL not configured (pts_upload_url is empty)")
        return None

    try:
        await _rate_limit()

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {settings.pts_api_token}",
                },
                files={"file": (filename, file_content)},
                data={"cat": "work_order"},
            )

        if resp.status_code == 401:
            logger.error("PTS upload failed: 401 Unauthorized (token invalid or expired)")
            return None
        if resp.status_code == 429:
            logger.warning("PTS upload rate limited (429)")
            return None
        if resp.status_code != 200:
            logger.error("PTS upload failed: HTTP %d: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        if data.get("err") != 0:
            logger.error("PTS upload error: err=%s, msg=%s", data.get("err"), data.get("msg"))
            return None

        file_id = data.get("id")
        logger.info("Uploaded %s to PTS via API: id=%s, filename=%s", filename, file_id, data.get("filename"))
        return file_id

    except Exception as e:
        logger.error("PTS file upload via API exception: %s", e)
        return None


async def download_and_upload_reports(
    report_attachments: list[dict],
) -> list[str]:
    """Download attachments from AITable and upload each to PTS.

    For each attachment in the list:
    1. Extract download URL (check both "url" and "downloadUrl" fields)
    2. Download the file content
    3. Upload to PTS via internal API
    4. Collect the PTS file IDs

    Args:
        report_attachments: AITable attachment list, each item is a dict with
            "filename", "url" or "downloadUrl", and optional "fileSize".

    Returns:
        List of PTS file IDs for successfully uploaded files.
        May be shorter than input list if some attachments fail.
    """
    file_ids: list[str] = []

    for att in report_attachments:
        if not isinstance(att, dict):
            continue

        filename = att.get("filename", "report.pdf")
        # AITable may return "url" or "downloadUrl" depending on the field type
        url = att.get("url") or att.get("downloadUrl", "")
        if not url:
            logger.warning("Attachment %s has no download URL, skipping", filename)
            continue

        try:
            # Download from AITable OSS
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                file_content = resp.content

            logger.info("Downloaded attachment: %s (%d bytes)", filename, len(file_content))

            # Upload to PTS
            pts_file_id = await upload_file_via_api(file_content, filename)
            if pts_file_id:
                file_ids.append(pts_file_id)
                logger.info("Uploaded %s to PTS: file_id=%s", filename, pts_file_id)
            else:
                logger.warning("Failed to upload %s to PTS, skipping", filename)

        except Exception as e:
            logger.error("Failed to download/upload attachment %s: %s", filename, e)
            continue

    return file_ids


async def upload_file(file_path: str, filename: str | None = None) -> str | None:
    """Upload a file to PTS file storage via browser automation.

    DEPRECATED: Use upload_file_via_api() instead, which uses the internal
    PTS API endpoint with Bearer token and does not require Playwright or
    session cookies. This method is kept for backward compatibility only.

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
        creator { id name username }
        delivery {
          id
          project { id name }
        }
      }
    }
    """ % pts_order_id
    result = await pts_graphql_query(query)
    return result.get("workOrderByID")


async def confirm_work_order_stage(work_order_id: str, claim_by: str | None = None) -> bool | None:
    """Advance a PTS work order to the next stage.

    PTS confirm_work_order_stage mutation returns Boolean (true on success, null on failure).
    May need to be called multiple times to advance through all stages to finished.

    Args:
        work_order_id: PTS work order ID.
        claim_by: Optional PTS user ID to set as responsible person.
            MUST be passed when advancing from the "指定工单负责人" stage,
            otherwise PTS returns "需要设置负责人" error.

    Returns:
        True on success, None/False on failure.
    """
    claim_part = ""
    if claim_by:
        claim_part = f', claim_by: "{claim_by}"'

    mutation = """
    mutation {
      confirm_work_order_stage(id: "%s"%s)
    }
    """ % (work_order_id, claim_part)

    result = await pts_graphql_query(mutation)
    return result.get("confirm_work_order_stage")


async def add_work_order_member(work_order_id: str, user_id: str) -> bool:
    """Add a user as a member to a PTS work order's delivery project.

    This is needed so that the user (舒磊) can operate on the work order
    (change claim_by, advance stage, etc.) even if they weren't originally
    a project member.

    Uses update_product_delivery_user_list mutation with the delivery ID.
    Note: update_work_order(id, input: {members}) does NOT work — PTS returns
    "Field 'members' is not defined by type 'WorkOrderUpdateParam'".

    Args:
        work_order_id: PTS work order ID.
        user_id: PTS user ID to add (e.g. 舒磊's ID).

    Returns:
        True on success, False on failure.
    """
    # First, get the delivery ID from the work order
    delivery_id = await _get_delivery_id(work_order_id)
    if not delivery_id:
        logger.warning("Cannot add member: no delivery ID for work order %s", work_order_id)
        return False

    mutation = """
    mutation {
      update_product_delivery_user_list(
        id: "%s",
        user: "%s"
      )
    }
    """ % (delivery_id, user_id)

    try:
        result = await pts_graphql_query(mutation)
        # Returns null on success (PTS convention for void mutations)
        if result.get("update_product_delivery_user_list") is not None:
            logger.info("Added user %s to delivery %s for work order %s", user_id, delivery_id, work_order_id)
            return True
        # null return can also mean success for PTS mutations
        logger.info("update_product_delivery_user_list returned null for delivery %s (likely success)", delivery_id)
        return True
    except Exception as e:
        logger.error("Failed to add member %s to delivery %s for work order %s: %s", user_id, delivery_id, work_order_id, e)
        return False


async def _get_delivery_id(work_order_id: str) -> str | None:
    """Get the delivery ID from a PTS work order.

    The delivery ID is needed for update_product_delivery_user_list
    (adding project members) and other delivery-level operations.
    """
    query = """
    {
      workOrderByID(id: \"%s\") {
        delivery { id }
      }
    }
    """ % work_order_id

    try:
        result = await pts_graphql_query(query)
        wo = result.get("workOrderByID") or {}
        delivery = wo.get("delivery") or {}
        return delivery.get("id")
    except Exception as e:
        logger.warning("Failed to get delivery ID for work order %s: %s", work_order_id, e)
        return None


async def query_work_order_details(pts_order_id: str) -> dict | None:
    """Query the complete PTS state needed by closure V2.

    This deliberately lives beside, rather than replacing, the smaller V1
    status query so existing callers keep their response contract.
    """
    query = """
    {
      workOrderByID(id: "%s") {
        id
        is_finished
        current_stage { name sequence }
        creator { id name username }
        claim_by { id name username }
        info { id note file { id filename } }
      }
    }
    """ % pts_order_id
    result = await pts_graphql_query(query)
    return result.get("workOrderByID") if result else None


async def download_attachment_with_hash(attachment: dict) -> dict:
    """Download an AITable attachment without retaining its URL in state/logs."""
    filename = str(attachment.get("filename") or "report.pdf")
    url = attachment.get("url") or attachment.get("downloadUrl") or ""
    if not url:
        return {
            "status": "failed",
            "filename": filename,
            "error": "附件缺少下载地址",
            "error_class": "permanent",
        }

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        content = response.content
        return {
            "status": "downloaded",
            "filename": filename,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content": content,
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return {
            "status": "failed",
            "filename": filename,
            "error": f"附件下载 HTTP {status}",
            "error_class": "retryable" if status == 429 or status >= 500 else "permanent",
        }
    except (httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
        logger.warning("Attachment download failed for %s: %s", filename, type(exc).__name__)
        return {
            "status": "failed",
            "filename": filename,
            "error": "附件下载网络异常",
            "error_class": "retryable",
        }


async def upload_file_via_api_with_retry(
    file_content: bytes,
    filename: str,
    *,
    max_retries: int | None = None,
    backoff_seconds: float | None = None,
    max_backoff_seconds: float | None = None,
) -> dict:
    """Upload a file and return a structured, retryable result for V2."""
    settings = get_settings()
    upload_url = settings.pts_upload_url
    if not upload_url:
        return {"status": "failed", "error": "PTS upload URL 未配置", "error_class": "permanent"}

    retries = settings.inspection_closure_upload_max_retries if max_retries is None else max_retries
    base_wait = settings.inspection_closure_retry_backoff_seconds if backoff_seconds is None else backoff_seconds
    max_wait = settings.inspection_closure_retry_max_backoff_seconds if max_backoff_seconds is None else max_backoff_seconds

    for attempt in range(retries + 1):
        try:
            await _rate_limit()
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {settings.pts_api_token}"},
                    files={"file": (filename, file_content)},
                    data={"cat": "work_order"},
                )

            status_code = response.status_code
            if status_code == 401:
                return {"status": "failed", "error": "PTS 上传认证失败", "error_class": "permission"}
            if status_code == 429 or status_code >= 500:
                if attempt < retries:
                    wait = min(max_wait, base_wait * (2 ** attempt))
                    logger.warning(
                        "PTS upload retryable HTTP %d for %s; retry %d/%d in %.1fs",
                        status_code, filename, attempt + 1, retries, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                return {
                    "status": "failed",
                    "error": f"PTS 上传 HTTP {status_code}",
                    "error_class": "retryable",
                }
            if status_code != 200:
                return {
                    "status": "failed",
                    "error": f"PTS 上传 HTTP {status_code}",
                    "error_class": "permanent",
                }

            data = response.json()
            if data.get("err") != 0 or not data.get("id"):
                return {
                    "status": "failed",
                    "error": "PTS 上传返回无效结果",
                    "error_class": "permanent",
                }
            return {"status": "uploaded", "pts_file_id": data["id"], "filename": filename}
        except (httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
            if attempt < retries:
                wait = min(max_wait, base_wait * (2 ** attempt))
                logger.warning(
                    "PTS upload network retry for %s (%s); retry %d/%d in %.1fs",
                    filename, type(exc).__name__, attempt + 1, retries, wait,
                )
                await asyncio.sleep(wait)
                continue
            return {"status": "failed", "error": "PTS 上传网络异常", "error_class": "retryable"}
        except Exception:
            logger.exception("PTS upload failed for %s", filename)
            return {"status": "failed", "error": "PTS 上传异常", "error_class": "permanent"}

    return {"status": "failed", "error": "PTS 上传重试耗尽", "error_class": "retryable"}
