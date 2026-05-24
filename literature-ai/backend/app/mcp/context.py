from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPAuthInfo:
    source_prefix: str
    display_name: str
    capabilities: frozenset[str]
    raw_key: str


_mcp_auth_context: ContextVar[MCPAuthInfo | None] = ContextVar("mcp_auth_context", default=None)


def get_mcp_auth() -> MCPAuthInfo | None:
    return _mcp_auth_context.get()


def set_mcp_auth(auth: MCPAuthInfo | None):
    return _mcp_auth_context.set(auth)


def reset_mcp_auth(token) -> None:
    _mcp_auth_context.reset(token)


@contextmanager
def mcp_auth_context(auth: MCPAuthInfo):
    token = set_mcp_auth(auth)
    try:
        yield
    finally:
        reset_mcp_auth(token)
