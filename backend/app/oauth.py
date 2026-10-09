# -*- coding: utf-8 -*-
"""OAuth 2.1 authorization server for the ChatGPT custom MCP connector.

Implements exactly the subset ChatGPT's connector needs, per the OpenAI
Apps/MCP OAuth integration contract and RFC 9728 protected-resource metadata:

- ``/.well-known/oauth-authorization-server``   discovery
- ``/.well-known/oauth-protected-resource``     protected resource metadata
- ``GET  /oauth/authorize``                     authorization code + PKCE (S256)
- ``POST /oauth/login``                         single-user login form
- ``POST /oauth/token``                         authorization_code / refresh_token

Single-user, predefined OAuth client (no DCR/CIMD).  Access tokens are HS256
JWTs carrying ``iss``/``aud``/``scope``/``exp``; the MCP auth middleware
(``app.mcp.auth``) accepts them first, then falls back to the legacy static
MCP API key so existing clients keep working.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.mcp.context import MCPAuthInfo

router = APIRouter()

OAUTH_CODE_TTL = 600          # authorization code lifetime (seconds)
ACCESS_TOKEN_TTL = 3600       # access token lifetime (seconds)
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # refresh token lifetime (seconds)

# In-memory single-instance stores. A backend restart invalidates outstanding
# codes/tokens, which simply prompts ChatGPT to re-authorize.
_auth_codes: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# Minimal HS256 JWT helpers (no external dependency)
# --------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _jwt_sign(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(
        hmac.new(secret.encode("utf-8"), f"{head}.{body}".encode("utf-8"), hashlib.sha256).digest()
    )
    return f"{head}.{body}.{signature}"


def _jwt_verify(token: str, secret: str) -> dict[str, Any] | None:
    try:
        head, body, signature = token.split(".")
        expected = _b64url(
            hmac.new(secret.encode("utf-8"), f"{head}.{body}".encode("utf-8"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("exp") and time.time() > int(payload["exp"]):
        return None
    return payload


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _scope_list() -> list[str]:
    raw = get_settings().oauth_scope_capabilities
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _authorization_servers() -> str:
    return get_settings().oauth_issuer


def _validate_authorize_params(params: dict[str, Any]) -> tuple[str, str]:
    """Validate an authorization request; return (redirect_uri, scope)."""
    settings = get_settings()
    if str(params.get("client_id", "")).strip() != settings.oauth_client_id:
        raise HTTPException(status_code=400, detail="invalid_client_id")
    allowed_redirects = [u.strip() for u in (settings.oauth_redirect_uri or "").split(",") if u.strip()]
    if str(params.get("redirect_uri", "")).strip() not in allowed_redirects:
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    if params.get("response_type") != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type")
    code_challenge = str(params.get("code_challenge", "")).strip()
    if not code_challenge:
        raise HTTPException(status_code=400, detail="missing_code_challenge")
    if params.get("code_challenge_method") not in (None, "", "S256", "plain"):
        raise HTTPException(status_code=400, detail="invalid_code_challenge_method")
    requested = [item for item in str(params.get("scope", "")).split() if item]
    allowed = set(_scope_list())
    if not all(item in allowed for item in requested):
        raise HTTPException(status_code=400, detail="invalid_scope")
    scope = " ".join(requested) if requested else " ".join(sorted(allowed))
    return str(params.get("redirect_uri", "")).strip(), scope


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if not code_verifier:
        return False
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        return hmac.compare_digest(_b64url(digest), code_challenge)
    # plain
    return hmac.compare_digest(code_verifier, code_challenge)


def _client_authenticated(request: Request, form: dict[str, str]) -> bool:
    """Validate the OAuth client. Supports client_secret_basic, post, and none."""
    settings = get_settings()
    if not settings.oauth_client_secret:
        return str(form.get("client_id", "")) == settings.oauth_client_id
    client_id = settings.oauth_client_id
    secret = settings.oauth_client_secret

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header.removeprefix("Basic ").strip()).decode("utf-8")
            provided_id, _, provided_secret = decoded.partition(":")
        except Exception:
            return False
        return provided_id == client_id and hmac.compare_digest(provided_secret, secret)

    provided_id = str(form.get("client_id", ""))
    provided_secret = str(form.get("client_secret", ""))
    if provided_id == client_id and provided_secret:
        return hmac.compare_digest(provided_secret, secret)
    if provided_id == client_id:
        # token endpoint auth method "none": acceptable for the predefined
        # single-user client (PKCE still protects the code exchange).
        return True
    return False


def _issue_tokens(scope: str, resource: str) -> tuple[str, str]:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iss": settings.oauth_issuer,
        "aud": resource or settings.oauth_resource,
        "sub": "user:liyuhao",
        "scope": scope,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "jti": secrets.token_hex(8),
        "token_type": "access",
    }
    access_token = _jwt_sign(payload, settings.oauth_jwt_secret)
    # Stateless refresh token: a self-contained JWT so that backend restarts
    # (docker compose up --force-recreate) never invalidate live clients.
    refresh_payload = {
        "iss": settings.oauth_issuer,
        "aud": resource or settings.oauth_resource,
        "sub": "user:liyuhao",
        "scope": scope,
        "iat": now,
        "exp": now + REFRESH_TOKEN_TTL,
        "jti": secrets.token_hex(8),
        "token_type": "refresh",
    }
    refresh_token = _jwt_sign(refresh_payload, settings.oauth_jwt_secret)
    return access_token, refresh_token


def _login_page(error: str | None = None, values: dict[str, str] | None = None) -> str:
    values = values or {}
    error_html = (
        f'<p style="color:#CF1322;font-size:13px;margin:0 0 10px;">{error}</p>' if error else ""
    )

    def hidden(name: str) -> str:
        value = values.get(name, "")
        escaped = (
            value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return f'<input type="hidden" name="{name}" value="{escaped}"/>'

    hidden_fields = "".join(
        hidden(name) for name in (
            "client_id", "redirect_uri", "response_type", "code_challenge",
            "code_challenge_method", "scope", "state", "resource",
        )
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Literature AI 授权</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Segoe UI',sans-serif;background:#F4F3EE;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.card{{background:#fff;border-radius:14px;padding:28px 32px;width:340px;box-sizing:border-box;border:1px solid #E4E3DD;}}
h1{{font-size:17px;margin:0 0 6px;color:#1A1B1C;}}
p.desc{{font-size:13px;color:#6B7280;margin:0 0 18px;line-height:1.5;}}
input[type=text],input[type=password]{{width:100%;box-sizing:border-box;padding:10px 12px;margin-bottom:12px;border:1px solid #D1D5DB;border-radius:8px;font-size:14px;}}
button{{width:100%;padding:11px;background:#1A1B1C;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;}}
button:hover{{opacity:.9;}}
</style>
</head>
<body>
<div class="card">
<h1>Literature AI 数据库授权</h1>
<p class="desc">允许 ChatGPT 连接你的文献数据库并调用 62 个工具（查询、图表审核、DFT 数据核对）。</p>
{error_html}
<form method="post" action="/oauth/login">
{hidden_fields}
<input type="text" name="username" placeholder="用户名" autocomplete="username"/>
<input type="password" name="password" placeholder="密码" autocomplete="current-password"/>
<button type="submit">授权并连接</button>
</form>
</div>
</body>
</html>"""


# --------------------------------------------------------------------------
# Discovery endpoints
# --------------------------------------------------------------------------

@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server() -> dict[str, Any]:
    settings = get_settings()
    issuer = settings.oauth_issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": _scope_list(),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource() -> dict[str, Any]:
    settings = get_settings()
    return {
        "resource": settings.oauth_resource,
        "authorization_servers": [_authorization_servers()],
        "scopes_supported": _scope_list(),
    }


# --------------------------------------------------------------------------
# Authorization endpoint + single-user login
# --------------------------------------------------------------------------

@router.get("/oauth/authorize")
async def oauth_authorize(request: Request) -> HTMLResponse:
    params = dict(request.query_params)
    redirect_uri, _scope = _validate_authorize_params(params)
    del redirect_uri, _scope
    return HTMLResponse(_login_page(values={k: str(v) for k, v in params.items()}))


@router.post("/oauth/login")
async def oauth_login(request: Request):
    form = await request.form()
    values = {key: str(form.get(key, "")) for key in (
        "client_id", "redirect_uri", "response_type", "code_challenge",
        "code_challenge_method", "scope", "state", "resource",
    )}
    settings = get_settings()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not settings.oauth_login_user or not settings.oauth_login_pass:
        raise HTTPException(status_code=503, detail="OAuth login not configured")
    if not hmac.compare_digest(username, settings.oauth_login_user) or not hmac.compare_digest(
        password, settings.oauth_login_pass
    ):
        return HTMLResponse(
            _login_page(error="用户名或密码错误", values=values),
            status_code=401,
        )

    try:
        redirect_uri, scope = _validate_authorize_params(values)
    except HTTPException as exc:
        return HTMLResponse(
            _login_page(error=f"授权参数无效：{exc.detail}", values=values),
            status_code=400,
        )

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": settings.oauth_client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": str(values.get("code_challenge", "")).strip(),
        "code_challenge_method": str(values.get("code_challenge_method") or "plain").strip() or "plain",
        "scope": scope,
        "resource": str(values.get("resource") or settings.oauth_resource).strip(),
        "exp": time.time() + OAUTH_CODE_TTL,
    }
    query = urllib.parse.urlencode({"code": code, "state": str(values.get("state", ""))})
    return RedirectResponse(url=f"{redirect_uri}?{query}", status_code=302)


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------

@router.post("/oauth/token")
async def oauth_token(request: Request):
    form = await request.form()
    form_values = {key: str(value) for key, value in form.items()}
    settings = get_settings()

    if not _client_authenticated(request, form_values):
        raise HTTPException(status_code=401, detail="invalid_client")

    grant_type = form_values.get("grant_type")
    if grant_type == "authorization_code":
        code = form_values.get("code", "")
        record = _auth_codes.pop(code, None)
        if record is None:
            raise HTTPException(status_code=400, detail="invalid_grant")
        if time.time() > record["exp"]:
            raise HTTPException(status_code=400, detail="invalid_grant")
        if form_values.get("redirect_uri") != record["redirect_uri"]:
            raise HTTPException(status_code=400, detail="invalid_grant")
        if not _verify_pkce(
            form_values.get("code_verifier", ""),
            record["code_challenge"],
            record["code_challenge_method"],
        ):
            raise HTTPException(status_code=400, detail="invalid_grant")

        access_token, refresh_token = _issue_tokens(record["scope"], record["resource"])
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": refresh_token,
            "scope": record["scope"],
        }

    if grant_type == "refresh_token":
        refresh_token = form_values.get("refresh_token", "")
        settings = get_settings()
        payload = _jwt_verify(refresh_token, settings.oauth_jwt_secret)
        if payload is None or payload.get("token_type") != "refresh":
            raise HTTPException(status_code=400, detail="invalid_grant")
        if payload.get("iss") != settings.oauth_issuer:
            raise HTTPException(status_code=400, detail="invalid_grant")
        if time.time() > int(payload.get("exp", 0)):
            raise HTTPException(status_code=400, detail="invalid_grant")
        scope = str(payload.get("scope", "")).strip()
        resource = str(payload.get("aud", "")).strip() or settings.oauth_resource
        access_token, new_refresh_token = _issue_tokens(scope, resource)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh_token,
            "scope": scope,
        }

    raise HTTPException(status_code=400, detail="unsupported_grant_type")


# --------------------------------------------------------------------------
# Token verification used by the MCP auth middleware
# --------------------------------------------------------------------------

def verify_oauth_access_token(token: str) -> MCPAuthInfo | None:
    """Validate an OAuth access token; return an authenticated MCP identity or None."""
    settings = get_settings()
    if not settings.oauth_jwt_secret:
        return None
    payload = _jwt_verify(token, settings.oauth_jwt_secret)
    if payload is None:
        return None
    if payload.get("iss") != settings.oauth_issuer:
        return None
    if payload.get("aud") != settings.oauth_resource:
        return None
    allowed = set(_scope_list())
    capabilities = frozenset(
        item for item in str(payload.get("scope", "")).split() if item in allowed
    )
    if not capabilities:
        return None
    return MCPAuthInfo(
        source_prefix="chatgpt_oauth",
        display_name="ChatGPT OAuth",
        capabilities=capabilities,
        raw_key="",
        source_identity="mcp:chatgpt-oauth",
        identity_verified=True,
    )
