"""Stateless signed-token helpers (HMAC-SHA256, stdlib only).

Used to sign auth-related cookies (OAuth state + session) without a
server-side session store. Token format:

    <base64url(payload_json)>.<base64url(hmac_sha256(payload))>

Secret comparison is constant-time. No third-party dependencies.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(raw: str) -> bytes | None:
    try:
        pad = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + pad)
    except Exception:
        return None


def sign_token(
    payload: dict,
    secret: str,
    ttl_seconds: int | None = None,
    now: int | None = None,
) -> str:
    """Sign a payload dict into a stateless token, optionally expiring."""
    if not secret:
        raise ValueError("signing secret must not be empty")
    now_i = int(now if now is not None else time.time())
    data = dict(payload)
    if ttl_seconds is not None:
        data["exp"] = now_i + int(ttl_seconds)
    body_raw = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    body = _b64e(body_raw)
    sig = _b64e(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{sig}"


def verify_token(
    token: str | None,
    secret: str,
    now: int | None = None,
) -> dict | None:
    """Verify and decode a signed token. Returns the payload or None."""
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    expected = _b64e(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(sig, expected):
        return None
    raw = _b64d(body)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if exp is not None:
        now_i = int(now if now is not None else time.time())
        if now_i > int(exp):
            return None
        data.pop("exp", None)
    return data


def generate_state() -> str:
    """Random state value for OAuth2 CSRF protection."""
    return secrets.token_urlsafe(24)
