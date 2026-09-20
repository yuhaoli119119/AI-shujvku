"""Real workbench authentication endpoints (``/api/auth/*``).

* ``POST /api/auth/login``   username + password -> HttpOnly session cookie
* ``POST /api/auth/logout``  revoke the session and clear cookies
* ``GET  /api/auth/me``      current identity (used by the login page)
* ``GET  /api/auth/verify``  internal endpoint used by nginx ``auth_request``

Credentials come from the existing Apache htpasswd file that used to back the
HTTP Basic gate, so there is exactly one place to add or change an account
(``scripts/litai_auth_user.py``).  Passwords are only ever stored as hashes and
are never written to logs.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.security.htpasswd import hash_password, verify_password
from app.security.rate_limit import FixedWindowRateLimiter
from app.security.session_auth import (
    apply_session_cookies,
    clear_session_cookies,
    client_ip,
    create_session,
    htpasswd_store,
    load_session,
    revoke_session,
    sessions_available,
)

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_limiter = FixedWindowRateLimiter()

# Constant-ish work for unknown accounts so response timing does not reveal
# whether the username exists.
_DUMMY_HASH = hash_password("litai-dummy-password-for-timing", "litai0001")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    remember: bool = False


def _rate_limit_settings() -> tuple[int, int, int]:
    settings = get_settings()
    window = max(30, int(getattr(settings, "auth_login_window_seconds", 900) or 900))
    per_ip_user = max(1, int(getattr(settings, "auth_login_max_failures_per_ip_user", 8) or 8))
    per_ip = max(1, int(getattr(settings, "auth_login_max_failures_per_ip", 30) or 30))
    return window, per_ip_user, per_ip


def _rate_limit_keys(ip: str, username: str) -> tuple[str, str]:
    return f"login:ip:{ip}", f"login:ip:{ip}:user:{username.strip().lower()}"


def _rate_limited_response(retry_after: int, scope: str) -> JSONResponse:
    logger.warning("Login rate limit hit (scope=%s) retry_after=%ss", scope, retry_after)
    return JSONResponse(
        {"detail": "rate_limited", "scope": scope, "retry_after": retry_after},
        status_code=429,
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _login_payload(state, csrf: str) -> dict:
    now = int(time.time())
    return {
        "username": state.username,
        "csrf_token": csrf,
        "expires_at": state.expires_at,
        "expires_in": max(0, state.expires_at - now),
        "remember": state.remember,
        "session_ttl_seconds": state.ttl,
    }


@router.post("/login")
async def login(payload: LoginRequest, request: Request):
    settings = get_settings()
    if not sessions_available(settings):
        logger.error(
            "Login rejected: session authentication is not configured "
            "(need LITAI_AUTH_SESSION_SECRET and a readable htpasswd file)"
        )
        return JSONResponse({"detail": "auth_not_configured"}, status_code=503)

    username = payload.username.strip()
    ip = client_ip(request)
    window, per_ip_user, per_ip = _rate_limit_settings()
    ip_key, ip_user_key = _rate_limit_keys(ip, username)

    ip_state = await _limiter.peek(ip_key, per_ip, window)
    if not ip_state.allowed:
        return _rate_limited_response(ip_state.retry_after, "ip")
    pair_state = await _limiter.peek(ip_user_key, per_ip_user, window)
    if not pair_state.allowed:
        return _rate_limited_response(pair_state.retry_after, "ip_user")

    store = htpasswd_store(settings)
    hash_field = store.hash_for(username)
    if hash_field is None:
        verify_password(_DUMMY_HASH, payload.password)
        authenticated = False
    else:
        authenticated = verify_password(hash_field, payload.password)

    if not authenticated:
        await _limiter.hit(ip_key, per_ip, window)
        decision = await _limiter.hit(ip_user_key, per_ip_user, window)
        # Deliberately identical for "no such user" and "wrong password".
        logger.warning("Login failed for user=%r from ip=%s", username, ip)
        if not decision.allowed:
            return _rate_limited_response(decision.retry_after, "ip_user")
        return JSONResponse({"detail": "invalid_credentials"}, status_code=401)

    await _limiter.reset(ip_user_key)
    issued = create_session(username, remember=payload.remember, settings=settings)
    response = JSONResponse(_login_payload(issued.state, issued.csrf), status_code=200)
    apply_session_cookies(response, issued, settings)
    logger.info("Login succeeded for user=%r from ip=%s", username, ip)
    return response


@router.post("/logout")
async def logout(request: Request):
    settings = get_settings()
    state = await load_session(request, settings=settings)
    if state is not None:
        await revoke_session(state)
        logger.info("Logout for user=%r from ip=%s", state.username, client_ip(request))
    response = JSONResponse({"status": "logged_out"}, status_code=200)
    clear_session_cookies(response, settings)
    return response


@router.get("/me")
async def me(request: Request):
    settings = get_settings()
    if not sessions_available(settings):
        return JSONResponse({"detail": "auth_not_configured"}, status_code=503)
    state = await load_session(request, settings=settings)
    if state is None:
        return JSONResponse({"detail": "not_authenticated"}, status_code=401)
    return JSONResponse(_login_payload(state, state.csrf), status_code=200)


@router.get("/verify")
async def verify(request: Request):
    """Internal endpoint hit by nginx ``auth_request`` (never exposed publicly)."""
    settings = get_settings()
    if not sessions_available(settings):
        # Fail closed: without a readable user store nothing may be served.
        return JSONResponse({"detail": "auth_not_configured"}, status_code=401)
    state = await load_session(request, settings=settings)
    if state is None:
        return JSONResponse(
            {"detail": "not_authenticated"},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"username": state.username, "expires_at": state.expires_at},
        status_code=200,
        headers={
            "Cache-Control": "no-store",
            "X-LitAI-User": state.username,
            "X-LitAI-Session-Expires": str(state.expires_at),
        },
    )
