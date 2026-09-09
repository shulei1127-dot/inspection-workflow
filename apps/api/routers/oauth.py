"""OAuth2/OIDC login router against the company IdP (auth.chaitin.net).

Flow: /oauth/login -> IdP authorize -> /oauth/callback -> exchange code for
access_token -> /userinfo -> signed session cookie -> back to app.
"""

import logging
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import security
from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

SESSION_COOKIE = "iw_session"
COOKIE_PATH = "/"


def _denied_page(title: str, message: str) -> HTMLResponse:
    """无权限/失败提示页：避免把错误弹回登录页造成重定向死循环。"""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,'PingFang SC',sans-serif;background:#f5f6f8;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="background:#fff;border-radius:8px;padding:40px;max-width:520px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.08)">
    <h2 style="margin-top:0">{title}</h2>
    <p style="color:#555;line-height:1.6">{message}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=403)


def sanitize_next_path(path: str) -> str:
    """Return a safe local redirect target (no open redirect)."""
    if not path:
        return "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    if "://" in path:
        return "/"
    return path[:512] or "/"


def _is_https_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _require_oidc_enabled():
    settings = get_settings()
    if settings.oidc_enabled:
        return settings
    return None


@router.get("/oauth/login")
async def oauth_login(request: Request, next: str = "/", error: str | None = None):
    """Redirect the user to the company IdP for login."""
    settings = _require_oidc_enabled()
    if settings is None:
        return HTMLResponse("认证未启用（OIDC_ENABLED=false）", status_code=503)
    if error:
        # 带 error 回跳说明 IdP 已拒绝（如 access_denied），直接展示结果避免循环
        return _denied_page(
            "认证未通过",
            "您暂时无法访问主动服务自动化系统。请在企业认证平台申请访问权限，"
            "或联系应用管理员审批通过后再试。",
        )

    safe_next = sanitize_next_path(next)
    secret = settings.oidc_cookie_signing_secret()
    # 无状态 state：签名后随授权请求带给 IdP，回调时验签即可。
    # 不依赖 cookie，因此支持"从 IP 发起登录 -> IdP -> 域名回调"的跨主机流程。
    state_token = security.sign_token(
        {"next": safe_next},
        secret,
        ttl_seconds=settings.oidc_state_ttl_seconds,
    )
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scope,
        "state": state_token,
    }
    auth_url = f"{settings.oidc_authorize_url}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url, status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Handle the IdP redirect: verify state, exchange code, create session."""
    settings = _require_oidc_enabled()
    if settings is None:
        return HTMLResponse("认证未启用（OIDC_ENABLED=false）", status_code=503)
    if error:
        logger.warning("OAuth callback denied by IdP (error=%s)", error)
        return _denied_page(
            "认证未通过",
            "公司统一认证未授权您访问此系统（access_denied）。"
            "请在企业认证平台申请访问权限，或联系应用管理员审批。",
        )
    if not code or not state:
        logger.warning("OAuth callback missing code/state")
        return _denied_page("登录失败", "回调缺少必要参数，请返回首页重新登录。")

    secret = settings.oidc_cookie_signing_secret()
    parsed = security.verify_token(state, secret)
    if not parsed:
        logger.warning("OAuth callback state mismatch")
        return _denied_page("登录失败", "安全校验未通过，请返回首页重新登录。")
    safe_next = sanitize_next_path(str(parsed.get("next") or "/"))

    timeout = httpx.Timeout(10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token_resp = await client.post(
                settings.oidc_token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.oidc_redirect_uri,
                    # 注册方式实测为 client_secret_post（凭据走 POST body）
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                },
            )
            if token_resp.status_code != 200:
                logger.error("OAuth token exchange failed: %s %s",
                             token_resp.status_code, token_resp.text[:300])
                return _denied_page("登录失败", "与统一认证服务通信失败，请稍后重试。")
            tokens = token_resp.json()
            access_token = tokens.get("access_token")
            if not access_token:
                return _denied_page("登录失败", "未获取到有效令牌，请稍后重试。")
            userinfo_resp = await client.get(
                settings.oidc_userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code != 200:
                logger.error("OAuth userinfo failed: %s", userinfo_resp.status_code)
                return _denied_page("登录失败", "获取用户信息失败，请稍后重试。")
            user = userinfo_resp.json()
    except httpx.HTTPError as exc:
        logger.error("OAuth IdP request error: %s", exc)
        return _denied_page("登录失败", "与统一认证服务连接失败，请稍后重试。")

    sub = str(user.get("id") or user.get("sub") or "").strip()
    if not sub:
        logger.error("OAuth userinfo missing id/sub: %s", str(user)[:300])
        return _denied_page("登录失败", "用户信息不完整，请稍后重试。")

    session = {
        "sub": sub,
        "username": str(user.get("username") or ""),
        "name": str(user.get("name") or ""),
        "email": str(user.get("email") or ""),
    }
    ttl = settings.oidc_session_ttl_hours * 3600
    session_token = security.sign_token(session, secret, ttl_seconds=ttl)
    response = RedirectResponse(safe_next, status_code=302)
    secure = _is_https_request(request)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        path=COOKIE_PATH,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    logger.info("OAuth login success user=%s", session["username"] or session["sub"])
    return response


# IdP 注册的回调地址是 /callback（与 /oauth/callback 同一处理逻辑）
router.add_api_route("/callback", oauth_callback, methods=["GET"], include_in_schema=False)


@router.get("/oauth/logout")
async def oauth_logout(request: Request):
    """Clear session cookies and go back to the login entry."""
    response = RedirectResponse("/oauth/login", status_code=302)
    secure = _is_https_request(request)
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=secure)
    return response


@router.get("/api/oauth/me")
async def oauth_me(request: Request):
    """Return the current session user (used by the SPA header/logs)."""
    settings = _require_oidc_enabled()
    if settings is None:
        return JSONResponse({"authenticated": False, "enabled": False})
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {"authenticated": True, "user": user}
