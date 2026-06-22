"""Settings API — user-facing configuration management.

All API keys and service configuration are stored in the active runtime
database, normally PostgreSQL in this project, so users never need to edit
.env files manually.

The ``Settings`` pydantic model still reads from env-vars at startup
(via ``pydantic-settings``), but this API allows runtime updates that
persist across restarts by writing key-value pairs into a dedicated
``app_settings`` table.  On startup, the application reads any persisted
overrides from that table and patches the cached Settings instance.

Security: API keys are stored as-is in the runtime database.  The GET endpoint masks
sensitive values (anything containing "key" or "secret").
"""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.security.owner import require_owner_request
from app.services.ide_prompt_service import (
    CANONICAL_MCP_PATH,
    PROMPT_SCHEMA_VERSION,
    build_ide_review_prompt,
    prompt_contract,
)

router = APIRouter()

_PROTOCOL_FILES = [
    {
        "key": "dft_results",
        "title": "DFT 结果提取",
        "path": "prompts/dft_results.yaml",
        "scope": "吸附能、Gibbs 自由能、反应能垒、电荷、DOS 等计算结果",
    },
    {
        "key": "dft_ai_protocol",
        "title": "DFT/材料数据 AI 解析协议",
        "path": "prompts/dft_ai_protocol.yaml",
        "scope": "全文证据阅读、AI-A/AI-B/Judge、候选抽取字段、去重、完整性审计与入库闸门",
    },
    {
        "key": "gemini_audit_protocol",
        "title": "Gemini/第二 AI 审核协议",
        "path": "prompts/gemini_audit_protocol.yaml",
        "scope": "检查候选 DFT 数据是否被 PDF 证据支持，并输出 accept/reject/needs_fix 等审核结论",
    },
    {
        "key": "dft_settings",
        "title": "DFT 设置与结构参数",
        "path": "prompts/dft_settings.yaml",
        "scope": "软件、泛函、赝势/基组、截断能、k 点、收敛、真空层等",
    },
    {
        "key": "mechanism_claims",
        "title": "机理声明提取",
        "path": "prompts/mechanism_claims.yaml",
        "scope": "多硫化物吸附、LiPS 转化、Li2S 成核/分解、穿梭抑制等机理",
    },
    {
        "key": "paper_writer",
        "title": "论文写作协议",
        "path": "prompts/paper_writer.yaml",
        "scope": "写作引用、证据约束、段落生成和不可引用/未核验内容的阻断规则",
    },
    {
        "key": "writing_card",
        "title": "写作卡提取",
        "path": "prompts/writing_card.yaml",
        "scope": "论文类型、研究空白、解决方案、证据链、图逻辑与段落策略",
    },
]


def _extract_protocol_meta(raw_text: str) -> dict[str, str | None]:
    def grab(key: str) -> str | None:
        match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", raw_text)
        return match.group(1).strip() if match else None

    return {
        "name": grab("name"),
        "version": grab("version"),
        "stage": grab("stage"),
    }


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
# Persistent settings helpers (runtime database table ``app_settings``)
# ---------------------------------------------------------------------------

def _get_active_engine():
    """Return the currently active database engine.

    Use ``get_settings().database_url`` as the PostgreSQL source of truth.
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
    return {
        "backend": backend or "rule",
        "model": model or "N/A",
        "configured": False,
        "disabled": True,
        "has_api_base": bool(api_base),
        "has_api_key": bool(api_key),
        "missing": [],
        "message": "网页端写作/解析模型已停用；解析、核对和审阅请通过 IDE / MCP AI 执行。",
    }


def _internal_parser_status_from_writer(writer_status: dict[str, Any]) -> dict[str, Any]:
    return {
        "configured": False,
        "disabled": True,
        "backend": "ide_mcp",
        "model": "IDE/MCP AI",
        "uses": "ide_mcp_ai",
        "missing": [],
        "message": "网页端解析已停用；请使用 prepare-ai-context / codex-item / import_analysis 由 IDE AI 回写结果。",
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
        "mcp_api_keys": "mcp_api_keys",
        "owner_api_token": "owner_api_token",
        "exports_enabled": "exports_enabled",
        "local_ingest_roots": "local_ingest_roots",
        "share_max_page_size": "share_max_page_size",
        "share_rate_limit_per_minute": "share_rate_limit_per_minute",
        "share_max_concurrency": "share_max_concurrency",
        "share_public_base_url": "share_public_base_url",
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
                current = getattr(settings, field_name)
                if isinstance(current, bool):
                    coerced = str(value).strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(current, int):
                    coerced = int(value)
                else:
                    coerced = value
                object.__setattr__(settings, field_name, coerced)
            except (ValueError, TypeError):
                object.__setattr__(settings, field_name, value)
        else:
            os.environ.pop(env_key, None)


def sync_writer_settings_from_session(session, settings) -> dict[str, str]:
    """Compatibility no-op: deprecated web-side writer settings stay disabled."""
    return {}


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
    "mcp_api_keys",
    "owner_api_token",
    "exports_enabled",
    "local_ingest_roots",
    "share_max_page_size",
    "share_rate_limit_per_minute",
    "share_max_concurrency",
    "share_public_base_url",
]

_DEPRECATED_WEB_AI_KEYS = {
    "writer_backend",
    "writer_model",
    "writer_api_base",
    "writer_api_key",
}

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _clean_base_url(value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    if base_url.endswith("/mcp"):
        base_url = base_url[:-4].rstrip("/")
    return base_url


def _split_host_port(netloc: str) -> tuple[str, int | None]:
    parsed = urlparse("//" + str(netloc or "").strip().rsplit("@", 1)[-1])
    host = parsed.hostname or str(netloc or "").strip().rsplit("@", 1)[-1].split(":", 1)[0].strip("[]")
    try:
        port = parsed.port
    except ValueError:
        port = None
    return host, port


def _is_probable_container_bridge_host(host: str) -> bool:
    """Return true for Docker/WSL-style bridge IPs that IDEs often cannot reach."""

    try:
        ip = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return ip in ipaddress.ip_network("172.16.0.0/12")


def _mcp_runner_command(request: Request | None) -> str:
    """Pick the command form most MCP clients can spawn on the user's OS."""

    user_agent = ""
    if request is not None:
        user_agent = str(request.headers.get("user-agent") or "").lower()
    if "windows" in user_agent or os.name == "nt":
        return "npx.cmd"
    return "npx"


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

    return ip.is_loopback


def _is_local_request_target(request: Request) -> bool:
    host_header = (request.headers.get("host") or "").strip()
    host = host_header.rsplit("@", 1)[-1].split(":", 1)[0].strip("[]")
    if _is_local_request_host(host):
        return True

    for header_name in ("origin", "referer"):
        header_value = (request.headers.get(header_name) or "").strip()
        if not header_value:
            continue
        parsed = urlparse(header_value)
        if _is_local_request_host(parsed.hostname or ""):
            return True
    return False


def _advertised_base_url(
    request: Request | None,
    *,
    fallback_host: str = "localhost",
    fallback_port: int = 8000,
) -> str:
    """Build an MCP URL that the user's IDE can actually reach.

    Socket probing inside Docker often returns an internal bridge IP such as
    172.x, so prefer the browser/request host used to open Literature AI.
    """

    explicit_base_url = os.environ.get("LITAI_PUBLIC_BASE_URL") or os.environ.get("LITAI_MCP_PUBLIC_BASE_URL")
    if explicit_base_url:
        return _clean_base_url(explicit_base_url)

    if request is not None:
        host_header = (request.headers.get("host") or "").strip()
        scheme = (request.headers.get("x-forwarded-proto") or getattr(getattr(request, "url", None), "scheme", "") or "http").split(",", 1)[0].strip()
        if host_header:
            host, port = _split_host_port(host_header)
            if _is_probable_container_bridge_host(host):
                return f"{scheme}://localhost:{port or fallback_port}"
            return f"{scheme}://{host_header.rstrip('/')}"

        for header_name in ("origin", "referer"):
            header_value = (request.headers.get(header_name) or "").strip()
            if not header_value:
                continue
            parsed = urlparse(header_value)
            if parsed.scheme and parsed.netloc:
                if _is_probable_container_bridge_host(parsed.hostname or ""):
                    return f"{parsed.scheme}://localhost:{parsed.port or fallback_port}"
                return f"{parsed.scheme}://{parsed.netloc}"

    host = str(fallback_host or "localhost").strip().strip("/")
    if _is_probable_container_bridge_host(host):
        host = "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{fallback_port}"


def _enforce_settings_write_access(request: Request) -> None:
    require_owner_request(request)


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
        if item.key in _DEPRECATED_WEB_AI_KEYS:
            kv_pairs[item.key] = None
            continue
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

    writer_status = _writer_status_from_values(
        backend="disabled",
        api_base=None,
        api_key=None,
        model="IDE/MCP AI",
    )

    # MCP status
    mcp_status = {
        "enabled": settings.mcp_enabled,
        "allow_unauthenticated": False,
        "has_keys": bool(persisted.get("mcp_api_keys") or settings.mcp_api_keys),
        "default_policy": (
            "disabled unless explicitly enabled for trusted local/dev use"
            if not settings.mcp_enabled
            else "bearer key required"
        ),
    }

    return {
        "embedding": embedding_status,
        "writer": writer_status,
        "internal_parser": _internal_parser_status_from_writer(writer_status),
        "mcp": mcp_status,
    }


@router.get("/extraction-protocols")
async def get_extraction_protocols() -> dict[str, Any]:
    """Return the current extraction protocols shown in the settings UI."""
    from app.config import PROJECT_ROOT

    items = []
    for item in _PROTOCOL_FILES:
        path = Path(PROJECT_ROOT) / item["path"]
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raw_text = f"# 协议文件读取失败: {exc}"
        items.append(
            {
                **item,
                **_extract_protocol_meta(raw_text),
                "raw_text": raw_text,
            }
        )
    return {
        "schema_version": "extraction_protocols_v1",
        "items": items,
    }


@router.get("/ide-prompts")
async def get_ide_prompts(request: Request) -> dict[str, Any]:
    """Generate IDE connection prompts and MCP config snippets.

    Uses the browser/request host first, then falls back to detected local IP.
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
    settings = get_settings()
    base_url = _advertised_base_url(request, fallback_host=local_ip, fallback_port=8000)
    mcp_url = f"{base_url}{CANONICAL_MCP_PATH}"

    sample_key = "litmcp_your_key"
    auth_required = True

    server_config: dict[str, Any] = {
        "command": _mcp_runner_command(request),
        "args": [
            "-y", "mcp-remote",
            mcp_url,
            "--transport", "http-only",
            "--allow-http",
        ],
    }
    server_config["args"].extend(["--header", "Authorization:${LITAI_AUTH_HEADER}"])
    server_config["env"] = {
        "LITAI_AUTH_HEADER": f"Bearer {sample_key}",
    }

    cursor_config = {
        "mcpServers": {
            "literature-ai": server_config,
        }
    }

    import json
    auth_text = f"Bearer {sample_key}"
    legacy_review_prompt = (
        "You are an IDE AI reviewing papers inside the user's Literature AI project.\n"
        "Do not edit MCP config files unless the user explicitly asks you to configure MCP. First use the MCP tools already exposed by the current IDE/project session.\n"
        "Look for a project MCP server named literature-ai and tools such as query_papers, get_paper, get_codex_context, read_paper_page, import_analysis, recrop_figure, and create_figure_from_bbox.\n\n"
        "Required first step:\n"
        "- Inspect the current available MCP/tool list in the IDE. If literature-ai tools are already available, start the review directly with those tools.\n"
        "- If the tools are not visible, ask the user to reload/restart the IDE MCP session; do not rewrite mcp_config.json, do not invent a new server, and do not keep retrying stale 172.x addresses.\n"
        "- Only if the user explicitly asks for manual MCP setup, use this fallback information: MCP URL = "
        f"{mcp_url}; Auth = {auth_text}; config = {json.dumps(cursor_config, ensure_ascii=False)}.\n\n"
        "Core workflow rules:\n"
        "- The web-side writer/internal parser is disabled. Use MCP tools and import_analysis for review and write-back.\n"
        "- Do not only write an audit report. For evidence-backed non-DFT content, write fixes back with import_analysis(auto_apply_review_rules=true); later AI writes may overwrite earlier AI writes and no module write lock is required.\n"
        "- Non-DFT direct-write modules include metadata, sections, tables, figure metadata/captions/summaries, writing_cards, mechanism_claims, electrochemical_performance, catalyst_samples, notes, and relationships.\n"
        "- DFT is the hard safety boundary. Do not single-AI-final-approve dft_results or dft_settings. For DFT, create review/audit/correction candidates and keep export behind explicit evidence review.\n"
        "- Figure image/crop operations are direct MCP actions only: use recrop_figure or create_figure_from_bbox. Do not submit bbox/image crop requests through import_analysis.\n"
        "- Use review_figure when you need to record a figure verdict such as verified, needs_repair, or rejected. Use import_analysis when you need to correct figure_role, content_summary, key_elements, page, or caption metadata.\n"
        "- For auto-apply, prefer structured evidence dicts. object_review_audits.evidence_location and correction_proposals.evidence_payload should include anchor keys such as page, table, figure, section, quoted_text, bbox, or evidence_text.\n"
        "- For RAG-ready facts, preserve source_type, source_id, paper_code, page, evidence_text, review_status, and evidence_locator when available.\n"
        "- Raw parser sections and parser-derived writing cards are not trusted knowledge. They must not be shown as final content or used by RAG/writing until an IDE AI review writes back ai_reviewed/ai_applied content with PDF evidence.\n"
        "- Catalyst samples must have a material identity and evidence anchor before being used for writing/RAG or linked to mechanism, electrochemical, or DFT records.\n"
        "- Writing support should use writing_cards, mechanism_claims, electrochemical_performance, catalyst_samples, figure cards, and verified DFT candidates where allowed by the safety gate.\n"
        "- Do not overwrite English evidence fields with Chinese translations. Put Chinese only in derived *_zh fields where available, writing cards, or review notes.\n"
        "- Generate dynamic source_label values such as <agent_name>_overall_<YYYYMMDD_HHMMSS>; never reuse a fixed date.\n\n"
        "Useful tools:\n"
        "- query_papers, get_paper, get_codex_context, get_codex_item, get_paper_knowledge, retrieve_evidence, read_paper_page\n"
        "- import_analysis, append_note, propose_correction, propose_dft_result_correction\n"
        "- get_dft_review_queue, verify_dft_result, reject_dft_result, approve_correction, reject_correction\n"
        "- recrop_figure, create_figure_from_bbox, review_figure, get_review_coverage, compare_papers, insert_word_citation\n"
    )

    return {
        "base_url": base_url,
        "mcp_url": mcp_url,
        "local_ip": local_ip,
        "hostname": hostname,
        "sample_key": sample_key,
        "auth_required": auth_required,
        "cursor_config": cursor_config,
        "cursor_config_json": json.dumps(cursor_config, indent=2, ensure_ascii=False),
        "vscode_config": cursor_config,  # Same format
        "vscode_config_json": json.dumps(cursor_config, indent=2, ensure_ascii=False),
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "prompt_contract": prompt_contract(),
        "suggested_prompt": build_ide_review_prompt("overall"),
        "legacy_english_suggested_prompt": legacy_review_prompt,
        "legacy_suggested_prompt": (
            f"请先使用你当前 IDE/项目会话已经暴露的 Literature AI MCP 工具，不要先改 mcp_config.json，也不要默认手工配置 MCP。\n"
            f"如果当前工具列表里已经有 literature-ai 相关工具，就直接开始；如果没有，再请用户重载/重启 IDE MCP 会话。\n"
            f"只有当用户明确要求手工配置 MCP 时，才使用下面的兜底信息：\n"
            f"服务地址：{mcp_url}\n"
            f"认证方式：Bearer {sample_key}\n"
            f"配置 JSON（仅在用户明确要求手工配置时使用）：\n"
            f"```json\n{json.dumps(cursor_config, indent=2, ensure_ascii=False)}\n```\n\n"
            f"连接成功后，你可以使用以下工具：\n"
            f"- query_papers：搜索论文\n"
            f"- get_paper：获取论文详情\n"
            f"- get_codex_context / get_codex_item：读取审核上下文\n"
            f"- read_paper_page：核对 PDF 原文页证据\n"
            f"- recrop_figure / create_figure_from_bbox：修复或创建图像证据\n"
            f"- review_figure：记录图表核验结论；需要写 verdict 时优先用它\n"
            f"- append_note：添加笔记\n"
            f"- propose_correction：提出修订建议\n"
            f"- import_analysis：除 DFT 最终确认外，按证据将 AI 结果写回；非 DFT 直接 auto_apply，后写覆盖先写，不需要申请模块写锁\n"
            f"- scan_local_pdfs：扫描本地 PDF\n"
            f"- ingest_pdf_batch：批量导入 PDF\n"
            f"- get_correction_queue：查看修订队列\n"
            f"- approve_correction / reject_correction：审批修订\n\n"
            f"如果当前项目工具已可用，就不要再回到手工配置流程。"
        ),
    }
