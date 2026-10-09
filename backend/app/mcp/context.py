from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class MCPAuthInfo:
    source_prefix: str
    display_name: str
    capabilities: frozenset[str]
    raw_key: str
    source_identity: str | None = None
    identity_verified: bool = False


def canonical_mcp_source_identity(source_prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", str(source_prefix or "").strip().casefold()).strip("-")
    if not normalized:
        raise ValueError("MCP source_prefix must not be empty")
    return f"mcp:{normalized}"


_mcp_auth_context: ContextVar[MCPAuthInfo | None] = ContextVar("mcp_auth_context", default=None)


def get_mcp_auth() -> MCPAuthInfo | None:
    return _mcp_auth_context.get()


def set_mcp_auth(auth: MCPAuthInfo | None):
    return _mcp_auth_context.set(auth)


def reset_mcp_auth(token) -> None:
    _mcp_auth_context.reset(token)


@contextmanager
def mcp_auth_context(credential: str):
    """Establish an authenticated in-process MCP context.

    Runtime callers must provide a configured MCP API key. Direct MCPAuthInfo
    injection is deliberately not supported here: callers that can construct
    Python objects must still prove identity with the same configured key path
    used by HTTP MCP.
    """
    from app.mcp.auth import authenticate_mcp_api_key

    auth = authenticate_mcp_api_key(credential)
    token = set_mcp_auth(auth)
    try:
        yield
    finally:
        reset_mcp_auth(token)
