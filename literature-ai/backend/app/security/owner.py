from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings


OWNER_SESSION_COOKIE = "litai_owner_session"
OWNER_SESSION_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class AuthenticatedOwnerIdentity:
    """Server-derived identity for operations that create final human truth."""

    actor: str
    source: str


def is_trusted_loopback_client(request: Request) -> bool:
    """Trust only the transport peer, never Host/Origin/Referer headers."""
    host = ((request.client.host if request.client else "") or "").strip()
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _provided_owner_token(request: Request) -> str:
    explicit = (
        request.headers.get("X-LitAI-Owner-Token", "")
        or request.headers.get("X-Settings-Token", "")
    ).strip()
    if explicit:
        return explicit
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return ""


def create_owner_session(
    provided_token: str,
    settings: Settings | None = None,
    *,
    now: int | None = None,
) -> tuple[str, AuthenticatedOwnerIdentity]:
    runtime = settings or get_settings()
    provided = str(provided_token or "").strip()
    candidates = (
        ((runtime.owner_api_token or "").strip(), AuthenticatedOwnerIdentity("owner", "owner_session")),
        ((runtime.settings_admin_token or "").strip(), AuthenticatedOwnerIdentity("settings_owner", "settings_session")),
    )
    for secret, identity in candidates:
        if secret and provided and hmac.compare_digest(provided, secret):
            expires_at = int(now if now is not None else time.time()) + OWNER_SESSION_TTL_SECONDS
            payload = f"v1:{identity.actor}:{expires_at}"
            signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
            encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
            return f"{payload}:{encoded}", identity
    if not any(secret for secret, _identity in candidates):
        raise HTTPException(status_code=401, detail="Owner authentication is not configured")
    raise HTTPException(status_code=403, detail="Invalid Owner token")


def authenticated_owner_session(
    request: Request,
    settings: Settings | None = None,
    *,
    now: int | None = None,
) -> AuthenticatedOwnerIdentity | None:
    cookies = getattr(request, "cookies", None) or {}
    value = str(cookies.get(OWNER_SESSION_COOKIE) or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    _version, actor, expires_raw, supplied_signature = parts
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if expires_at < int(now if now is not None else time.time()):
        return None
    runtime = settings or get_settings()
    candidates = {
        "owner": ((runtime.owner_api_token or "").strip(), AuthenticatedOwnerIdentity("owner", "owner_session")),
        "settings_owner": ((runtime.settings_admin_token or "").strip(), AuthenticatedOwnerIdentity("settings_owner", "settings_session")),
    }
    secret, identity = candidates.get(actor, ("", None))
    if not secret or identity is None:
        return None
    payload = f"v1:{actor}:{expires_at}"
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return identity if hmac.compare_digest(supplied_signature, expected) else None


def require_owner_request(request: Request, settings: Settings | None = None) -> None:
    if is_trusted_loopback_client(request):
        return
    runtime = settings or get_settings()
    if authenticated_owner_session(request, runtime) is not None:
        return
    configured = (runtime.owner_api_token or runtime.settings_admin_token or "").strip()
    provided = _provided_owner_token(request)
    if not configured:
        raise HTTPException(status_code=401, detail="Owner authentication is required")
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail="Invalid Owner token")


def require_authenticated_owner_request(
    request: Request,
    settings: Settings | None = None,
) -> AuthenticatedOwnerIdentity:
    """Require a configured owner credential even for loopback requests.

    The broad application boundary intentionally keeps loopback convenient for
    read and proposal workflows. Final human verification is different: a
    local AI process is also a loopback client, so transport location cannot be
    used as human identity.
    """

    runtime = settings or get_settings()
    owner_token = (runtime.owner_api_token or "").strip()
    settings_token = (runtime.settings_admin_token or "").strip()
    session_identity = authenticated_owner_session(request, runtime)
    if session_identity is not None:
        return session_identity
    provided = _provided_owner_token(request)
    if not owner_token and not settings_token:
        raise HTTPException(
            status_code=401,
            detail="Authenticated Owner identity is required for final verification",
        )
    if owner_token and provided and hmac.compare_digest(provided, owner_token):
        return AuthenticatedOwnerIdentity(actor="owner", source="owner_api_token")
    if settings_token and provided and hmac.compare_digest(provided, settings_token):
        return AuthenticatedOwnerIdentity(actor="settings_owner", source="settings_admin_token")
    if not provided:
        raise HTTPException(
            status_code=401,
            detail="Authenticated Owner identity is required for final verification",
        )
    raise HTTPException(status_code=403, detail="Invalid Owner token")


def _owner_protected_path(path: str) -> bool:
    if (
        path.startswith("/api/share/")
        or path == "/api/health"
        or path.startswith("/mcp")
        or path == "/api/settings/owner-session"
    ):
        return False
    return (
        path.startswith("/api")
        or path in {"/docs", "/redoc", "/openapi.json"}
    )


async def enforce_owner_boundary(request: Request, call_next):
    if not _owner_protected_path(request.url.path):
        return await call_next(request)
    try:
        require_owner_request(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)
