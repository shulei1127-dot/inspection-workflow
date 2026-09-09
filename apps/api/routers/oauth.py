"""OAuth2/OIDC login router against the company IdP (auth.chaitin.net).

Flow: /oauth/login -> IdP authorize -> /oauth/callback -> exchange code for
access_token -> /userinfo -> signed session cookie -> back to app.
"""

import logging
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import security
from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

SESSION_COOKIE = "iw_session"
STATE_COOKIE = "iw_oauth_state"
COOKIE_PATH = "/"


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
async def oauth_login(request: Request, next: str = "/"):
    """Redirect the user to the company IdP for login."""
    settings = _require_oidc_enabled()
    if settings is None:
        return HTMLResponse("认证未启用（OIDC_ENABLED=false）", status_code=503)

    state = security.generate_state()
    safe_next = sanitize_next_path(next)
    secret = settings.oidc_cookie_signing_secret()
    state_token = security.sign_token(
        {"state": state, "next": safe_next},
        secret,
        ttl_seconds=settings.oidc_state_ttl_seconds,
    )
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scope,
        "state": state,
    }
    auth_url = f"{settings.oidc_authorize_url}?{urllib.parse.urlencode(params)}"
    response = RedirectResponse(auth_url, status_code=302)
    secure = _is_https_request(request)
    response.set_cookie(
        STATE_COOKIE,
        state_token,
        path=COOKIE_PATH,
        max_age=settings.oidc_state_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    return response


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
    if error or not code or not state:
        logger.warning("OAuth callback missing code/state (error=%s)", error)
        return RedirectResponse("/oauth/login?error=denied", status_code=302)

    secret = settings.oidc_cookie_signing_secret()
    state_token = request.cookies.get(STATE_COOKIE)
    parsed = security.verify_token(state_token, secret) if state_token else None
    if not parsed or not secrets.compare_digest(str(parsed.get("state", "")), state):
        logger.warning("OAuth callback state mismatch")
        return RedirectResponse("/oauth/login?error=state", status_code=302)
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
                return RedirectResponse("/oauth/login?error=token", status_code=302)
            tokens = token_resp.json()
            access_token = tokens.get("access_token")
            if not access_token:
                return RedirectResponse("/oauth/login?error=token", status_code=302)
            userinfo_resp = await client.get(
                settings.oidc_userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_resp.status_code != 200:
                logger.error("OAuth userinfo failed: %s", userinfo_resp.status_code)
                return RedirectResponse("/oauth/login?error=userinfo", status_code=302)
            user = userinfo_resp.json()
    except httpx.HTTPError as exc:
        logger.error("OAuth IdP request error: %s", exc)
        return RedirectResponse("/oauth/login?error=network", status_code=302)

    sub = str(user.get("id") or user.get("sub") or "").strip()
    if not sub:
        logger.error("OAuth userinfo missing id/sub: %s", str(user)[:300])
        return RedirectResponse("/oauth/login?error=userinfo", status_code=302)

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
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH, secure=secure)
    logger.info("OAuth login success user=%s", session["username"] or session["sub"])
    return response


@router.get("/oauth/logout")
async def oauth_logout(request: Request):
    """Clear session cookies and go back to the login entry."""
    response = RedirectResponse("/oauth/login", status_code=302)
    secure = _is_https_request(request)
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=secure)
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH, secure=secure)
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
