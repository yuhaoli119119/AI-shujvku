import shutil
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel


router = APIRouter()


class SwitchDbPayload(BaseModel):
    database_url: str


@router.get("/db-info")
async def get_db_info() -> dict:
    from app.config import get_settings
    from app.utils.active_database import get_active_database_info

    settings = get_settings()
    info = get_active_database_info()

    return {
        "database_url_masked": info["db_url_masked"],
        "dialect": info["db_kind"],
        "db_path": info["db_path"],
        "storage_root": str(settings.storage_root),
        "active_library": info["active_library"],
        "active_library_db_path": info["active_library_db_path"],
        "is_active_library_sqlite": info["is_active_library_sqlite"],
        "matches_active_library_db_path": info["matches_active_library_db_path"],
    }


@router.post("/switch-db", deprecated=True)
async def switch_db(payload: SwitchDbPayload) -> dict:
    """已废弃。请改用 POST /api/libraries/{name}/activate 切换库。"""
    url = payload.database_url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="database_url is required")
    if not url.startswith("sqlite:///"):
        raise HTTPException(
            status_code=400,
            detail="For safety, only sqlite:/// URLs are supported",
        )
    from app.db.session import switch_database

    switch_database(url)
    return {"status": "ok", "database_url": url, "warning": "此 API 已废弃，请改用 POST /api/libraries/{name}/activate"}


@router.post("/upload-db", deprecated=True)
async def upload_db(file: UploadFile = File(...)) -> dict:
    """已废弃。请改用 POST /api/libraries 或 POST /api/libraries/import 管理库。"""
    if not file.filename or not file.filename.lower().endswith((".sqlite", ".db", ".sqlite3")):
        raise HTTPException(status_code=400, detail="只允许 SQLite 文件 (.sqlite, .db, .sqlite3)")

    upload_dir = Path("data/uploaded_dbs")
    upload_dir.mkdir(parents=True, exist_ok=True)

    dest_path = upload_dir / file.filename
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    database_url = f"sqlite:///{dest_path.resolve().as_posix()}"
    from app.db.session import switch_database

    switch_database(database_url)
    return {
        "status": "ok",
        "database_url": database_url,
        "filename": file.filename,
        "warning": "此 API 已废弃，请改用 POST /api/libraries 或 POST /api/libraries/import",
    }


@router.get("/agent-guide")
async def get_agent_guide() -> dict:
    return {
        "system_name": "Literature AI",
        "recommended_entrypoint": {
            "mode": "http_workflow",
            "description": "Use the one-shot AI workflow API for search, download, ingest, and parse.",
            "method": "POST",
            "path": "/api/papers/ai_workflow",
            "json_schema_hint": {
                "query": "string",
                "model": "string",
                "max_results": "int",
                "max_downloads": "int",
                "providers": ["openalex", "arxiv", "pubmed", "semantic_scholar"],
                "skip_existing": "bool",
            },
        },
        "http_endpoints": [
            {
                "name": "ai_workflow",
                "method": "POST",
                "path": "/api/papers/ai_workflow",
                "purpose": "Rewrite query with LLM, search, download candidate PDFs, ingest, and parse.",
            },
            {
                "name": "ai_search",
                "method": "POST",
                "path": "/api/papers/ai_search",
                "purpose": "Rewrite query with LLM and return discovery results without downloading.",
            },
            {
                "name": "discovery_search",
                "method": "GET",
                "path": "/api/papers/discovery/search",
                "purpose": "External literature search only.",
            },
            {
                "name": "discovery_download",
                "method": "POST",
                "path": "/api/papers/discovery/download",
                "purpose": "Download one discovery result and ingest/parse it.",
            },
            {
                "name": "list_papers",
                "method": "GET",
                "path": "/api/papers",
                "purpose": "List parsed papers already stored in the system.",
            },
            {
                "name": "get_paper",
                "method": "GET",
                "path": "/api/papers/{paper_id}",
                "purpose": "Get the full parsed detail for one paper.",
            },
        ],
        "mcp": {
            "url": "/mcp",
            "transport": "streamable_http",
            "auth": "Authorization: Bearer <mcp_api_key>",
            "recommended_when": "Use MCP when the client supports MCP tools and needs interactive paper reading, note taking, or correction proposals.",
            "common_tools": [
                "query_papers",
                "get_paper",
                "append_note",
                "propose_correction",
                "parse_paper",
                "get_parse_status",
            ],
        },
        "desktop_sync": {
            "desktop_url_setting": "Literature AI URL",
            "desktop_actions": [
                "AI自动查文献",
                "同步 LitAI 结果",
            ],
            "purpose": "After HTTP or MCP work completes, the desktop client can sync results back into the local project library.",
        },
        "llm_configuration": {
            "env_prefix": "LITAI_",
            "required_for_live_llm": [
                "LITAI_WRITER_BACKEND=openai_compatible",
                "LITAI_WRITER_MODEL=<model_name>",
                "LITAI_WRITER_API_BASE=<openai_compatible_base_url>",
                "LITAI_WRITER_API_KEY=<api_key>",
            ],
        },
        "suggested_client_prompt": (
            "First call GET /api/system/agent-guide. "
            "Then prefer POST /api/papers/ai_workflow for automatic literature search/download/parse. "
            "After completion, inspect GET /api/papers or GET /api/papers/{paper_id}."
        ),
    }
