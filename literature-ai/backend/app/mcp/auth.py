from __future__ import annotations

from dataclasses import dataclass
import ipaddress

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.mcp.context import MCPAuthInfo, get_mcp_auth, reset_mcp_auth, set_mcp_auth


ALL_MCP_CAPABILITIES = frozenset(
    {
        "read_papers",
        "append_notes",
        "propose_corrections",
        "request_parse",
        "review_corrections",
        "review_dft",
    }
)


@dataclass(frozen=True)
class MCPKeyConfig:
    source_prefix: str
    display_name: str
    raw_key: str
    capabilities: frozenset[str]


def parse_mcp_api_keys(raw: str) -> dict[str, MCPKeyConfig]:
    configs: dict[str, MCPKeyConfig] = {}
    for item in [part.strip() for part in raw.split(";") if part.strip()]:
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 4:
            continue
        source_prefix, display_name, api_key, capability_blob = parts
        capabilities = frozenset(
            capability.strip() for capability in capability_blob.split(",") if capability.strip()
        )
        configs[api_key] = MCPKeyConfig(
            source_prefix=source_prefix,
            display_name=display_name,
            raw_key=api_key,
            capabilities=capabilities,
        )
    return configs


def _request_source_is_trusted(request: Request) -> bool:
    client_host = (request.client.host if request.client else "").strip()
    if not client_host:
        return False
    if client_host in {"localhost", "testclient"}:
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _unauthenticated_mcp_allowed(request: Request) -> bool:
    settings = get_settings()
    if not settings.mcp_enabled:
        return False
    if not _request_source_is_trusted(request):
        return False
    configured_keys = parse_mcp_api_keys(settings.mcp_api_keys)
    if configured_keys:
        return False
    return settings.mcp_allow_unauthenticated


def _anonymous_mcp_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="open_mcp",
        display_name="Open MCP",
        capabilities=ALL_MCP_CAPABILITIES,
        raw_key="",
    )


def authenticate_mcp_request(request: Request) -> MCPAuthInfo:
    settings = get_settings()
    configured_keys = parse_mcp_api_keys(settings.mcp_api_keys)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if _unauthenticated_mcp_allowed(request):
            return _anonymous_mcp_auth()
        raise HTTPException(status_code=401, detail="Missing MCP API key")

    raw_key = auth_header.removeprefix("Bearer ").strip()
    config = configured_keys.get(raw_key)
    if not config:
        if _unauthenticated_mcp_allowed(request):
            return _anonymous_mcp_auth()
        raise HTTPException(status_code=401, detail="Invalid MCP API key")

    return MCPAuthInfo(
        source_prefix=config.source_prefix,
        display_name=config.display_name,
        capabilities=config.capabilities,
        raw_key=config.raw_key,
    )


async def enforce_mcp_auth(request: Request, call_next):
    if not request.url.path.startswith("/mcp"):
        return await call_next(request)

    try:
        auth = authenticate_mcp_request(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    token = set_mcp_auth(auth)
    try:
        return await call_next(request)
    finally:
        reset_mcp_auth(token)


def require_mcp_capability(capability: str) -> MCPAuthInfo:
    auth = get_mcp_auth()
    if auth is None:
        raise PermissionError("MCP authentication context is missing")
    if capability not in auth.capabilities:
        raise PermissionError(f"MCP key does not have capability: {capability}")
    return auth


def require_mcp_capability_any(*capabilities: str) -> MCPAuthInfo:
    """Check that the MCP key has at least one of the given capabilities.
    Used when multiple capability levels grant access (e.g. review_corrections
    is a superset of review_dft).
    """
    auth = get_mcp_auth()
    if auth is None:
        raise PermissionError("MCP authentication context is missing")
    if not any(cap in auth.capabilities for cap in capabilities):
        raise PermissionError(
            f"MCP key does not have any of the required capabilities: {', '.join(capabilities)}"
        )
    return auth


def get_request_mcp_auth(request: Request) -> MCPAuthInfo:
    return authenticate_mcp_request(request)


def require_request_mcp_capability(capability: str):
    def dependency(auth: MCPAuthInfo = Depends(get_request_mcp_auth)) -> MCPAuthInfo:
        if capability not in auth.capabilities:
            raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")
        return auth

    return dependency
