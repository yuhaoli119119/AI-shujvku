"""Settings API — user-facing configuration management.

All API keys and service configuration are stored in the active SQLite
database so that users never need to edit .env files manually.

The ``Settings`` pydantic model still reads from env-vars at startup
(via ``pydantic-settings``), but this API allows runtime updates that
persist across restarts by writing key-value pairs into a dedicated
``app_settings`` table.  On startup, the application reads any persisted
overrides from that table and patches the cached Settings instance.

Security: API keys are stored as-is in SQLite.  The GET endpoint masks
sensitive values (anything containing "key" or "secret").
"""
from __future__ import annotations

import ipaddress
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Sensitive key detection (for masking in GET responses)
# ---------------------------------------------------------------------------

_SENSITIVE_KEYWORDS = {"key", "secret", "password", "token"}


def _is_sensitive(key: str) -> bool:
    lower = key.lower()
    return any(kw in lower for kw in _SENSITIVE_KEYWORDS)


def _mask_value(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SettingItem(BaseModel):
    key: str
    value: str | None = None


class SettingsUpdateRequest(BaseModel):
    settings: list[SettingItem]


# ---------------------------------------------------------------------------
# Persistent settings helpers (SQLite table ``app_settings``)
# ---------------------------------------------------------------------------

def _get_active_engine():
    """Return the currently active database engine.

    Use ``get_settings().database_url`` as the source of truth.  The active
    library recovery path updates this value via ``switch_database``; picking an
    arbitrary cached engine can otherwise read settings from a stale shadow DB.
    """
    from app.config import get_settings
    from app.db.session import get_engine

    return get_engine(get_settings().database_url)


def _ensure_table() -> None:
    """Create the ``app_settings`` table if it does not exist."""
    engine = _get_active_engine()
    with engine.begin() as conn:
        conn.execute(
            __import__("sqlalchemy").text(
                "CREATE TABLE IF NOT EXISTS app_settings ("
                "  key   VARCHAR(255) PRIMARY KEY,"
                "  value TEXT"
                ")"
            )
        )


def _read_persisted_settings() -> dict[str, str]:
    """Read all persisted key-value pairs from the database."""
    from sqlalchemy import text

    _ensure_table()
    engine = _get_active_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT key, value FROM app_settings")).fetchall()
    return {row[0]: row[1] for row in rows}


def _read_persisted_settings_from_session(session) -> dict[str, str]:
    """Read persisted settings from the database bound to the current session."""
    from sqlalchemy import text

    try:
        rows = session.execute(text("SELECT key, value FROM app_settings")).fetchall()
    except Exception:
        return {}
    return {row[0]: row[1] for row in rows}


def _writer_status_from_values(
    *,
    backend: str | None,
    api_base: str | None,
    api_key: str | None,
    model: str | None,
) -> dict[str, Any]:
    missing = []
    if not api_base:
        missing.append("writer_api_base")
    if not api_key:
        missing.append("writer_api_key")
    if not model:
        missing.append("writer_model")
    configured = not missing
    return {
        "backend": backend or "rule",
        "model": model or "N/A",
        "configured": configured,
        "has_api_base": bool(api_base),
        "has_api_key": bool(api_key),
        "missing": missing,
        "message": "Writer LLM 已配置" if configured else "Writer LLM 尚未配置完整",
    }


def _internal_parser_status_from_writer(writer_status: dict[str, Any]) -> dict[str, Any]:
    missing_map = {
        "writer_api_base": "internal_parser_api_base",
        "writer_api_key": "internal_parser_api_key",
        "writer_model": "internal_parser_model",
    }
    missing = [missing_map.get(item, item) for item in writer_status.get("missing", [])]
    configured = bool(writer_status.get("configured"))
    return {
        "configured": configured,
        "backend": writer_status.get("backend", "rule"),
        "model": writer_status.get("model", "N/A"),
        "uses": "writer_llm",
        "missing": missing,
        "message": (
            "Internal parser LLM configured"
            if configured
            else "Internal AI parsing is not configured; it uses the Writer LLM connection, not Embedding."
        ),
    }


def _write_persisted_settings(kv_pairs: dict[str, str | None]) -> None:
    """Upsert key-value pairs into the database."""
    from sqlalchemy import text

    _ensure_table()
    engine = _get_active_engine()
    with engine.begin() as conn:
        for key, value in kv_pairs.items():
            if value is None:
                conn.execute(text("DELETE FROM app_settings WHERE key = :key"), {"key": key})
            else:
                conn.execute(
                    text(
                        "INSERT INTO app_settings (key, value) VALUES (:key, :value) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"key": key, "value": value},
                )


def _apply_settings_to_runtime(kv_pairs: dict[str, str | None]) -> None:
    """Apply persisted settings to the running Settings instance and env vars."""
    from app.config import get_settings

    settings = get_settings()

    # Mapping from db key → Settings field name
    key_to_field = {
        "embedding_provider": "embedding_provider",
        "embedding_api_base": "embedding_api_base",
        "embedding_api_key": "embedding_api_key",
        "embedding_model": "embedding_model",
        "embedding_dimension": "embedding_dimension",
        "writer_backend": "writer_backend",
        "writer_model": "writer_model",
        "writer_api_base": "writer_api_base",
        "writer_api_key": "writer_api_key",
        "mcp_api_keys": "mcp_api_keys",
    }

    for key, value in kv_pairs.items():
        field_name = key_to_field.get(key)
        if not field_name:
            continue
        # Update env var so future Settings() calls pick it up
        env_key = f"LITAI_{field_name.upper()}"
        if value is not None:
            os.environ[env_key] = value
            # Patch the cached instance directly
            try:
                object.__setattr__(settings, field_name, type(getattr(settings, field_name))(value))
            except (ValueError, TypeError):
                object.__setattr__(settings, field_name, value)
        else:
            os.environ.pop(env_key, None)


def sync_writer_settings_from_session(session, settings) -> dict[str, str]:
    """Apply persisted Writer settings from the current session's database."""
    persisted = _read_persisted_settings_from_session(session)
    if not persisted:
        return {}

    writer_keys = ("writer_backend", "writer_model", "writer_api_base", "writer_api_key")
    overrides = {key: persisted[key] for key in writer_keys if key in persisted}
    if not overrides:
        return {}

    _apply_settings_to_runtime(overrides)
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    return overrides


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

# The set of settings that users are allowed to view/edit via this API
_MANAGED_KEYS = [
    "embedding_provider",
    "embedding_api_base",
    "embedding_api_key",
    "embedding_model",
    "embedding_dimension",
    "writer_backend",
    "writer_model",
    "writer_api_base",
    "writer_api_key",
    "mcp_api_keys",
]

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_local_request_host(client_host: str) -> bool:
    client_host = (client_host or "").strip().lower()
    if not client_host:
        return False

    if client_host in _LOCAL_HOSTS:
        return True

    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False

    # Treat private/link-local Docker/WSL bridge addresses as local-mode access.
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _enforce_settings_write_access(request: Request) -> None:
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    provided_token = request.headers.get("X-Settings-Token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    configured_token = (settings.settings_admin_token or "").strip()
    client_host = (request.client.host if request.client else "") or ""

    if configured_token:
        if provided_token != configured_token:
            raise HTTPException(status_code=403, detail="Invalid settings admin token")
        return

    if not _is_local_request_host(client_host):
        raise HTTPException(
            status_code=403,
            detail="Settings writes are limited to local requests unless LITAI_SETTINGS_ADMIN_TOKEN is configured.",
        )


@router.get("")
async def get_settings_api() -> dict[str, Any]:
    """Return current settings, with sensitive values masked."""
    from app.config import get_settings

    settings = get_settings()
    persisted = _read_persisted_settings()

    result = {}
    for key in _MANAGED_KEYS:
        # Prefer persisted value, fall back to Settings default
        raw_value = persisted.get(key) or str(getattr(settings, key, "") or "")
        if _is_sensitive(key):
            result[key] = _mask_value(raw_value)
        else:
            result[key] = raw_value
    return result


@router.post("")
async def update_settings_api(request: SettingsUpdateRequest, raw_request: Request) -> dict[str, Any]:
    """Update settings. Only managed keys are accepted."""
    from app.config import get_settings

    _enforce_settings_write_access(raw_request)

    kv_pairs: dict[str, str | None] = {}
    for item in request.settings:
        if item.key not in _MANAGED_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown setting key: {item.key}")
        # If the value looks like a masked value (contains ****), skip it
        if item.value and "****" in item.value:
            continue
        kv_pairs[item.key] = item.value

    if not kv_pairs:
        return {"status": "ok", "updated": 0, "message": "没有需要更新的配置"}

    # Persist to DB
    _write_persisted_settings(kv_pairs)

    # Apply to runtime
    _apply_settings_to_runtime(kv_pairs)

    # Clear lru_cache so new calls get fresh settings
    get_settings.cache_clear()
    # Re-apply persisted settings to the new cached instance
    all_persisted = _read_persisted_settings()
    _apply_settings_to_runtime(all_persisted)

    return {"status": "ok", "updated": len(kv_pairs)}


@router.get("/status")
async def get_services_status() -> dict[str, Any]:
    """Return the connectivity status of each configured service."""
    from app.config import get_settings
    from app.services.embedding import get_embedding_service

    settings = get_settings()
    persisted = _read_persisted_settings()

    # Embedding status
    emb_provider = persisted.get("embedding_provider") or settings.embedding_provider
    emb_api_base = persisted.get("embedding_api_base") or settings.embedding_api_base
    emb_api_key = persisted.get("embedding_api_key") or settings.embedding_api_key
    emb_model = persisted.get("embedding_model") or settings.embedding_model

    embedding_status = {
        "provider": emb_provider,
        "model": emb_model or "N/A",
        "configured": bool(emb_api_base and emb_api_key) if emb_provider == "openai_compatible" else True,
    }

    # Writer status
    writer_api_base = persisted.get("writer_api_base") or settings.writer_api_base
    writer_api_key = persisted.get("writer_api_key") or settings.writer_api_key
    writer_model = persisted.get("writer_model") or settings.writer_model

    writer_status = _writer_status_from_values(
        backend=persisted.get("writer_backend") or settings.writer_backend,
        api_base=writer_api_base,
        api_key=writer_api_key,
        model=writer_model,
    )

    # MCP status
    mcp_status = {
        "enabled": settings.mcp_enabled,
        "has_keys": bool(persisted.get("mcp_api_keys") or settings.mcp_api_keys),
        "default_policy": "disabled unless explicitly enabled for trusted local/dev use" if not settings.mcp_enabled else "enabled",
    }

    return {
        "embedding": embedding_status,
        "writer": writer_status,
        "internal_parser": _internal_parser_status_from_writer(writer_status),
        "mcp": mcp_status,
    }


@router.get("/ide-prompts")
async def get_ide_prompts() -> dict[str, Any]:
    """Generate IDE connection prompts and MCP config snippets.

    Uses the current host IP/hostname to build correct URLs.
    """
    import socket

    # Detect local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    hostname = socket.gethostname()
    base_url = f"http://{local_ip}:8000"
    mcp_url = f"{base_url}/mcp"

    # Read persisted MCP keys to provide a sample key
    persisted = _read_persisted_settings()
    from app.config import get_settings

    settings = get_settings()
    mcp_keys_raw = persisted.get("mcp_api_keys") or settings.mcp_api_keys
    sample_key = "litmcp_your_key"
    if mcp_keys_raw:
        # Format: name|label|key|scopes;...
        first_entry = mcp_keys_raw.split(";")[0]
        parts = first_entry.split("|")
        if len(parts) >= 3:
            sample_key = parts[2]

    cursor_config = {
        "mcpServers": {
            "literature-ai": {
                "command": "npx",
                "args": [
                    "-y", "mcp-remote",
                    mcp_url,
                    "--transport", "http-only",
                    "--allow-http",
                    "--header", "Authorization:${LITAI_AUTH_HEADER}",
                ],
                "env": {
                    "LITAI_AUTH_HEADER": f"Bearer {sample_key}",
                },
            }
        }
    }

    import json

    return {
        "base_url": base_url,
        "mcp_url": mcp_url,
        "local_ip": local_ip,
        "hostname": hostname,
        "sample_key": sample_key,
        "cursor_config": cursor_config,
        "cursor_config_json": json.dumps(cursor_config, indent=2, ensure_ascii=False),
        "vscode_config": cursor_config,  # Same format
        "vscode_config_json": json.dumps(cursor_config, indent=2, ensure_ascii=False),
        "suggested_prompt": (
            f"我现在需要你连接我的 Literature AI 知识库系统来帮助我查文献。\n"
            f"连接方式：MCP 协议\n"
            f"服务地址：{mcp_url}\n"
            f"认证方式：Bearer {sample_key}\n\n"
            f"配置 JSON（直接写入你的 MCP 配置文件）：\n"
            f"```json\n{json.dumps(cursor_config, indent=2, ensure_ascii=False)}\n```\n\n"
            f"连接成功后，你可以使用以下工具：\n"
            f"- query_papers：搜索论文\n"
            f"- get_paper：获取论文详情\n"
            f"- append_note：添加笔记\n"
            f"- propose_correction：提出修订建议\n"
            f"- scan_local_pdfs：扫描本地 PDF\n"
            f"- ingest_pdf_batch：批量导入 PDF\n"
            f"- get_correction_queue：查看修订队列\n"
            f"- approve_correction / reject_correction：审批修订\n\n"
            f"请先确认连接成功，然后根据我的需求使用对应工具。"
        ),
    }
