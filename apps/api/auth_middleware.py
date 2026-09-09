"""Auth middleware: require an OIDC session cookie unless auth is disabled.

When OIDC_ENABLED=false every request passes through untouched, so enabling
the feature is a pure runtime toggle (no code redeploy required to switch
the gate on/off).
"""

import logging
import urllib.parse

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core import security
from core.config import get_settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "iw_session"

# Paths reachable without a session (OAuth entry/exit + health).
_PUBLIC_EXACT = {"/oauth/login", "/oauth/callback", "/oauth/logout", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/api/health",)


def is_public_path(path: str) -> bool:
    """Return True when the request path may be reached anonymously."""
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _is_trusted_local_client(host: str | None) -> bool:
    """Loopback / docker bridge callers (host ops, scheduler probes) are trusted."""
    if not host:
        return False
    if host in {"127.0.0.1", "::1", "localhost"}:
        return True
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "172":
        try:
            return 16 <= int(parts[1]) <= 31
        except ValueError:
            return False
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect browsers to /oauth/login; answer API callers with 401 JSON."""

    async def dispatch(self, request, call_next):
        settings = get_settings()
        if not settings.oidc_enabled:
            return await call_next(request)
        # Only gate HTTP requests; leave WebSocket/other ASGI scopes alone.
        if request.scope.get("type") != "http":
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or is_public_path(path):
            return await call_next(request)
        if _is_trusted_local_client(request.client.host if request.client else None):
            return await call_next(request)

        secret = settings.oidc_cookie_signing_secret()
        token = request.cookies.get(SESSION_COOKIE)
        user = security.verify_token(token, secret) if token else None
        if user:
            request.state.user = {
                k: user.get(k)
                for k in ("sub", "username", "name", "email")
            }
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "authentication required", "login_url": "/oauth/login"},
                status_code=401,
            )
        query = request.url.query
        target = urllib.parse.quote(path + (f"?{query}" if query else ""))
        return RedirectResponse(f"/oauth/login?next={target}", status_code=302)
