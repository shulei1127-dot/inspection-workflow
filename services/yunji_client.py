"""Yunji API client: direct HTTP calls with session cookie.

Replaces the Puppeteer-based approach from server.js:
- PTS data: fetched via GraphQL API (pts_client.py)
- Yunji API: direct HTTP calls using session cookie
- Cookie keepalive: periodic refresh by visiting yunji pages

Authentication: yunji.chaitin.cn requires TWO cookies:
- yunji_session_id: session identifier
- go-server-token: server-side token

Both must be set in YUNJI_SESSION_COOKIE as a full Cookie header value,
e.g. "yunji_session_id=xxx; go-server-token=yyy"

Use node extract_cookie.js to extract from the Chrome session.
"""

import asyncio
import json
import logging
import time

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

YUNJI_BASE = "https://yunji.chaitin.cn"

# Rate limiting for yunji API
_yunji_last_call: float = 0.0
_yunji_rate_lock = asyncio.Lock()
_YUNJI_RATE_INTERVAL = 0.3  # ~3 req/s


async def _rate_limit() -> None:
    global _yunji_last_call
    async with _yunji_rate_lock:
        now = time.monotonic()
        elapsed = now - _yunji_last_call
        if elapsed < _YUNJI_RATE_INTERVAL:
            await asyncio.sleep(_YUNJI_RATE_INTERVAL - elapsed)
        _yunji_last_call = time.monotonic()


def _get_cookie() -> str:
    settings = get_settings()
    return settings.yunji_session_cookie


def _make_headers() -> dict:
    cookie = _get_cookie()
    return {
        "Content-Type": "application/json",
        "Cookie": cookie if cookie else "",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Referer": "https://yunji.chaitin.cn/",
        "Accept": "application/json, text/plain, */*",
    }


async def yunji_api(method: str, path: str, body: dict | None = None) -> dict:
    """Call yunji REST API directly via HTTP.

    Args:
        method: HTTP method (GET/POST)
        path: API path, e.g. /api/admin/requirement/crm_project_info
        body: Request body for POST

    Returns:
        API response data field

    Raises:
        RuntimeError: If API returns error or cookie is missing
        PermissionError: If session cookie is expired
    """
    cookie = _get_cookie()
    if not cookie:
        raise PermissionError("云集 session cookie 未配置，请设置 YUNJI_SESSION_COOKIE")

    await _rate_limit()

    url = f"{YUNJI_BASE}{path}"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        kwargs = {
            "method": method,
            "url": url,
            "headers": _make_headers(),
        }
        if body is not None:
            kwargs["json"] = body
        resp = await client.request(**kwargs)

    if resp.status_code in (301, 302, 303, 307):
        # Redirect likely means session expired
        location = resp.headers.get("location", "")
        if "login" in location or "sso" in location:
            raise PermissionError("云集 session 已过期，请更新 YUNJI_SESSION_COOKIE")
        raise RuntimeError(f"云集 API 重定向: {location}")

    if resp.status_code == 401 or resp.status_code == 403:
        raise PermissionError("云集 session 已过期或无效，请更新 YUNJI_SESSION_COOKIE")

    if resp.status_code != 200:
        raise RuntimeError(f"云集 API 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    error_code = data.get("errorCode")
    if error_code and error_code != 0:
        error_msg = data.get("message", json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"云集 API 错误 [{path}]: {error_msg}")

    return data.get("data", data)


# ── Cookie Keepalive ──────────────────────────────────────────────────────


async def keepalive_cookie() -> dict:
    """Visit yunji page to keep the session cookie alive.

    Should be called periodically (e.g. every 2-3 hours) by the scheduler.
    """
    cookie = _get_cookie()
    if not cookie:
        return {"status": "skipped", "reason": "云集 session cookie 未配置"}

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            resp = await client.get(
                f"{YUNJI_BASE}/",
                headers={
                    "Cookie": cookie,
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                },
            )
            if resp.status_code in (301, 302, 303, 307):
                location = resp.headers.get("location", "")
                if "login" in location or "sso" in location:
                    return {"status": "expired", "message": "Session cookie 已过期，请更新 YUNJI_SESSION_COOKIE"}
            return {"status": "ok", "http_status": resp.status_code}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def verify_session() -> dict:
    """Verify the yunji session cookie is valid by calling a lightweight API."""
    cookie = _get_cookie()
    if not cookie:
        return {"valid": False, "reason": "云集 session cookie 未配置"}

    try:
        # Try a lightweight API call
        result = await yunji_api("GET", "/api/admin/partner/select_options")
        return {"valid": True, "partners_count": len(result) if isinstance(result, list) else "unknown"}
    except PermissionError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        return {"valid": False, "reason": str(e)}
