from __future__ import annotations

import hmac
import ipaddress

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings


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


def require_owner_request(request: Request, settings: Settings | None = None) -> None:
    if is_trusted_loopback_client(request):
        return
    runtime = settings or get_settings()
    configured = (runtime.owner_api_token or runtime.settings_admin_token or "").strip()
    provided = _provided_owner_token(request)
    if not configured:
        raise HTTPException(status_code=401, detail="Owner authentication is required")
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail="Invalid Owner token")


def _owner_protected_path(path: str) -> bool:
    if path.startswith("/api/share/") or path == "/api/health" or path.startswith("/mcp"):
        return False
    return (
        path.startswith("/api")
        or (path.startswith("/pages/") and not path.startswith("/pages/share/"))
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
