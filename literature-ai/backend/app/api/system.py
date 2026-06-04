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
        "configured_db_kind": info.get("configured_db_kind"),
        "configured_db_url_masked": info.get("configured_db_url_masked"),
        "source_of_truth": info.get("source_of_truth"),
        "force_configured_database": info.get("force_configured_database"),
        "storage_root": str(settings.storage_root),
        "active_library": info["active_library"],
        "active_library_db_path": info["active_library_db_path"],
        "is_active_library_sqlite": info["is_active_library_sqlite"],
        "matches_active_library_db_path": info["matches_active_library_db_path"],
        "effective_db_path": info.get("effective_db_path"),
        "effective_storage_root": info.get("effective_storage_root"),
        "effective_db_papers_total": info.get("effective_db_papers_total"),
        "effective_matches_active_library_db_path": info.get("effective_matches_active_library_db_path"),
        "recovered_from_candidate_scan": info.get("recovered_from_candidate_scan"),
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
        "positioning": (
            "A local literature toolbench for Codex-centered workflows. "
            "The system stores papers, parses PDFs into readable artifacts, exposes evidence and structured candidates, "
            "and lets Codex or a human decide what is reliable."
        ),
        "recommended_entrypoint": {
            "mode": "codex_mcp_first",
            "description": "Connect through MCP first so Codex can query papers, read full parsed records, retrieve evidence, append notes, and propose corrections. Use batch ingestion only as an optional acquisition helper.",
            "method": "MCP",
            "path": "/mcp",
            "json_schema_hint": {
                "read_tools": ["query_papers", "get_paper", "get_codex_context", "get_codex_item", "get_paper_knowledge", "get_dft_review_queue", "retrieve_evidence", "compare_papers"],
                "curation_tools": ["append_note", "propose_correction", "propose_dft_result_correction", "import_analysis", "verify_dft_result", "reject_dft_result"],
                "ingestion_tools": ["scan_local_pdfs", "ingest_pdf_batch", "parse_paper"],
                "writing_tools": ["insert_word_citation"],
            },
        },
        "http_endpoints": [
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
            {
                "name": "get_codex_context",
                "method": "GET",
                "path": "/api/papers/{paper_id}/codex-context",
                "purpose": "Get a compact Codex-ready JSON and Markdown paper bundle.",
            },
            {
                "name": "get_codex_item",
                "method": "GET",
                "path": "/api/papers/{paper_id}/codex-item/{item_type}/{item_id}",
                "purpose": "Get low-token context, evidence locators, and safety state for one paper item.",
            },
            {
                "name": "get_paper_knowledge",
                "method": "GET",
                "path": "/api/papers/{paper_id}/knowledge-context",
                "purpose": "Get Codex-ready knowledge candidates from mechanism claims, writing cards, external AI imports, notes, and section fallbacks.",
            },
            {
                "name": "verify_dft_result",
                "method": "POST",
                "path": "/api/papers/{paper_id}/dft-results/{result_id}/verify",
                "purpose": "Promote one evidence-backed DFT candidate after explicit Codex/human PDF review confirmation.",
            },
            {
                "name": "reject_dft_result",
                "method": "POST",
                "path": "/api/papers/{paper_id}/dft-results/{result_id}/reject",
                "purpose": "Reject a bad DFT candidate after explicit Codex/human curation so it leaves the active review queue.",
            },
            {
                "name": "propose_dft_result_correction",
                "method": "POST",
                "path": "/api/papers/{paper_id}/dft-results/{result_id}/corrections",
                "purpose": "Create a pending correction proposal for one DFT result field without applying it.",
            },
            {
                "name": "get_dft_review_queue",
                "method": "GET",
                "path": "/api/papers/export/dft-review-queue",
                "purpose": "List DFT candidates that need evidence/locator/review work before ML export.",
            },
            {
                "name": "retrieval_search",
                "method": "POST",
                "path": "/api/retrieval/search",
                "purpose": "Retrieve relevant evidence from parsed papers for Codex review and writing support.",
            },
            {
                "name": "insert_word_citation",
                "method": "POST",
                "path": "/api/writing/word/insert-citation",
                "purpose": "Upload a DOCX and generate a copy with a guarded draft citation inserted from the local literature database.",
            },
            {
                "name": "ai_workflow",
                "method": "POST",
                "path": "/api/papers/ai_workflow",
                "purpose": "Optional batch acquisition helper: rewrite query with LLM, search, download candidate PDFs, ingest, and parse.",
            },
            {
                "name": "ai_search",
                "method": "POST",
                "path": "/api/papers/ai_search",
                "purpose": "Optional discovery helper: rewrite query with LLM and return search results without downloading.",
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
        ],
        "mcp": {
            "url": "/mcp",
            "transport": "streamable_http",
            "auth": "Authorization: Bearer <mcp_api_key>",
            "recommended_when": "Use MCP as the primary Codex interface for interactive paper reading, evidence retrieval, note taking, imported analysis, comparison, and correction proposals.",
            "common_tools": [
                "query_papers",
                "get_paper",
                "get_codex_context",
                "get_codex_item",
                "get_paper_knowledge",
                "get_dft_review_queue",
                "retrieve_evidence",
                "compare_papers",
                "insert_word_citation",
                "append_note",
                "propose_correction",
                "propose_dft_result_correction",
                "import_analysis",
                "verify_dft_result",
                "reject_dft_result",
                "parse_paper",
                "scan_local_pdfs",
                "ingest_pdf_batch",
                "get_parse_status",
            ],
        },
        "desktop_sync": {
            "desktop_url_setting": "Literature AI URL",
            "desktop_actions": [
                "辅助批量查文献",
                "同步 LitAI 结果",
            ],
            "purpose": "After HTTP or MCP work completes, the desktop client can sync candidate results back into the local project library.",
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
            "Then connect to /mcp and prefer query_papers, get_dft_review_queue, get_codex_context, get_codex_item, get_paper_knowledge, get_paper, retrieve_evidence, compare_papers, insert_word_citation for guarded DOCX citation copies, append_note, propose_correction, propose_dft_result_correction for field fixes, verify_dft_result after explicit evidence review, and reject_dft_result for bad candidates. "
            "Use /api/papers/ai_workflow only when batch acquisition is explicitly needed."
        ),
    }
