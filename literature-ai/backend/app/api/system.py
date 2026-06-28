from fastapi import APIRouter

from app.mcp.auth import parse_mcp_api_keys, validate_mcp_capability_assignments
from app.services.ide_prompt_service import (
    CANONICAL_MCP_PATH,
    PROMPT_SCHEMA_VERSION,
    build_ide_review_prompt,
    prompt_contract,
)


router = APIRouter()


@router.get("/db-info")
async def get_db_info() -> dict:
    from app.config import get_settings
    from app.utils.active_database import get_active_database_info

    settings = get_settings()
    info = get_active_database_info()
    configured_db_papers_total = info.get("configured_db_papers_total")
    effective_db_papers_total = info.get("effective_db_papers_total")
    if info.get("db_kind") == "postgresql":
        try:
            from sqlalchemy import text

            from app.db.session import get_engine

            with get_engine(settings.database_url).connect() as connection:
                configured_db_papers_total = int(
                    connection.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0
                )
                if info.get("active_library"):
                    effective_db_papers_total = int(
                        connection.execute(
                            text("SELECT COUNT(*) FROM papers WHERE library_name = :library_name"),
                            {"library_name": info["active_library"]},
                        ).scalar()
                        or 0
                    )
                else:
                    effective_db_papers_total = configured_db_papers_total
        except Exception:
            pass

    return {
        "database_url_masked": info["db_url_masked"],
        "dialect": info["db_kind"],
        "storage_root": str(settings.storage_root),
        "active_library": info["active_library"],
        "active_library_root": info.get("active_library_root"),
        "papers_total": effective_db_papers_total,
        "configured_db_papers_total": configured_db_papers_total,
    }


@router.get("/agent-guide")
async def get_agent_guide() -> dict:
    from app.config import get_settings

    settings = get_settings()
    mcp_capability_warnings = validate_mcp_capability_assignments(parse_mcp_api_keys(settings.mcp_api_keys))
    return {
        "system_name": "Literature AI",
        "positioning": (
            "A local literature toolbench for Codex-centered workflows. "
            "The system stores papers, parses PDFs into readable artifacts, exposes evidence and structured candidates, "
            "and lets Codex or a human decide what is reliable. "
            "The database is PostgreSQL with the pgvector extension."
        ),
        "recommended_entrypoint": {
            "mode": "codex_mcp_first",
            "description": "Connect through MCP first so Codex can query papers, read full parsed records, retrieve evidence, append notes, and propose corrections. Use batch ingestion only as an optional acquisition helper.",
            "method": "MCP",
            "path": CANONICAL_MCP_PATH,
            "json_schema_hint": {
                "read_tools": ["query_papers", "get_paper", "get_codex_context", "get_codex_item", "get_paper_knowledge", "search_external_papers", "get_dft_review_queue", "get_dft_audit_issues", "get_correction_queue", "retrieve_evidence", "compare_papers", "read_paper_page", "review_figure", "get_review_coverage", "get_field_disputes", "scan_duplicate_dois"],
                "curation_tools": ["append_note", "propose_correction", "propose_dft_result_correction", "repair_dft_audit_issue", "import_analysis", "update_table", "create_table", "delete_table", "merge_table", "verify_dft_result", "reject_dft_result", "verify_dft_results_batch", "reject_dft_results_batch", "approve_correction", "reject_correction", "approve_corrections_batch", "reject_corrections_batch", "export_ml_dataset", "recrop_figure"],
                "ingestion_tools": ["scan_local_pdfs", "ingest_pdf_batch", "parse_paper", "get_parse_status", "recrop_figure"],
                "writing_tools": ["insert_word_citation"],
            },
        },
        "http_endpoints": [
            {
                "name": "prepare_external_ai_context",
                "method": "POST",
                "path": "/api/papers/{paper_id}/prepare-ai-context",
                "purpose": "Refresh AI-readable materials, evidence bundles, workspace files, and codex context so IDE/MCP AI can continue parsing or auditing without requiring a backend LLM.",
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
                "name": "get_dft_audit_report",
                "method": "GET",
                "path": "/api/dft/audit-report",
                "purpose": "Read-only DFT audit/repair health report grouped by issue status, issue type, repair action, actor, and capability diagnostics.",
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
                "name": "literature_intake",
                "method": "POST",
                "path": "/api/intake/search",
                "purpose": "Controlled literature intake: search external sources into review candidates only; users must approve candidates before any download or ingest job can start.",
            },
            {
                "name": "ai_search",
                "method": "POST",
                "path": "/api/papers/ai_search",
                "purpose": "Discovery helper that uses the raw query and returns search results without downloading; web-side LLM query rewriting is disabled.",
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
            "url": CANONICAL_MCP_PATH,
            "transport": "streamable_http",
            "auth": "HTTP MCP requires Authorization: Bearer <mcp_api_key>. The in-process fallback uses mcp_auth_context instead of HTTP authentication.",
            "capability_warnings": mcp_capability_warnings,
            "key_role_examples": [
                {
                    "source_prefix": "ide_ai",
                    "display_name": "IDE AI",
                    "sample_key": "litmcp_ide_ai",
                    "capabilities": ["read_papers", "append_notes", "propose_corrections", "request_parse"],
                    "purpose": "ordinary IDE AI can read context and submit unverified notes/proposals/audit candidates",
                },
                {
                    "source_prefix": "assigned_dft_audit",
                    "display_name": "Assigned DFT Audit AI",
                    "sample_key": "litmcp_assigned_dft_audit",
                    "capabilities": ["read_papers", "propose_corrections"],
                    "purpose": "DFT audit AI can create issue/candidate evidence but must not repair DFT audit issues",
                },
                {
                    "source_prefix": "dft_primary_repair",
                    "display_name": "DFT Primary Repair AI",
                    "sample_key": "litmcp_dft_primary_repair",
                    "capabilities": ["read_papers", "repair_dft_issues"],
                    "purpose": "primary repair AI can read DFT audit issues and repair one issue at a time",
                },
                {
                    "source_prefix": "human_reviewer",
                    "display_name": "Human Reviewer",
                    "sample_key": "litmcp_human_reviewer",
                    "capabilities": ["read_papers", "review_corrections", "review_dft"],
                    "purpose": "trusted human/admin reviewer may verify or reject through explicit review tools; DFT issue repair remains separate",
                },
            ],
            "recommended_when": "Use MCP as the primary Codex interface for interactive paper reading, evidence retrieval, note taking, imported analysis, comparison, and correction proposals.",
            "common_tools": [
                "query_papers",
                "get_paper",
                "get_codex_context",
                "get_codex_item",
                "get_paper_knowledge",
                "search_external_papers",
                "get_dft_review_queue",
                "get_dft_audit_issues",
                "retrieve_evidence",
                "compare_papers",
                "read_paper_page",
                "review_figure",
                "get_review_coverage",
                "get_field_disputes",
                "insert_word_citation",
                "append_note",
                "propose_correction",
                "propose_dft_result_correction",
                "repair_dft_audit_issue",
                "import_analysis",
                "update_table",
                "create_table",
                "delete_table",
                "merge_table",
                "verify_dft_result",
                "reject_dft_result",
                "verify_dft_results_batch",
                "reject_dft_results_batch",
                "approve_correction",
                "reject_correction",
                "approve_corrections_batch",
                "reject_corrections_batch",
                "export_ml_dataset",
                "parse_paper",
                "scan_local_pdfs",
                "ingest_pdf_batch",
                "get_parse_status",
                "recrop_figure",
                "create_figure_from_bbox",
                "create_share_token",
                "scan_duplicate_dois",
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
        "ai_workflow": {
            "mode": "ide_mcp_only",
            "note": "Web-side model execution is disabled. Use IDE AI over MCP to read materials and import candidates.",
        },
        "ingestion_config": {
            "auto_run_stage2_extraction": settings.auto_run_stage2_extraction,
            "description": "When True, ingestion may still create backend candidate outputs. Regardless of that setting, the supported manual recovery path is to prepare AI-readable materials (workspace, evidence, codex context, AI reading package) and let IDE AI continue via MCP/import_analysis instead of requiring any backend-owned LLM deep parse.",
        },
        "safety_boundaries": {
            "dft_page_locators": [
                "DFT rows with paper provenance, source section, and evidence text but no exact PDF page must remain text_only candidates.",
                "Do not infer exact PDF pages from approximate similarity, nearby figures, or section-title matches.",
                "Do not expose a web UI AI page-lookup action unless a real audited backend workflow exists.",
                "Ad hoc page investigation belongs to the assigned IDE AI when requested by the user; any proposed page remains a candidate until reviewed.",
                "Missing-page repair must not mark DFT rows verified, approve corrections, bind materials, or unlock CSV/ML export.",
            ],
        },
        "legacy_suggested_client_prompt": (
            "First call GET /api/system/agent-guide. "
            "Then inspect the current IDE/project MCP tool list and use the already exposed literature-ai tools first. Do not rewrite mcp_config.json or invent a new MCP server unless the user explicitly asks for manual MCP setup. Only if the current project session truly does not expose literature-ai should you reconnect to /mcp/ with a configured Bearer key. Prefer query_papers, search_external_papers to discover new literature from OpenAlex/arXiv, get_dft_review_queue, get_dft_audit_issues, get_codex_context, get_codex_item, get_paper_knowledge, get_paper, retrieve_evidence, compare_papers, append_note, propose_correction, propose_dft_result_correction for field fixes, repair_dft_audit_issue for single DFT audit issue primary-AI repair, verify_dft_result after explicit evidence review, reject_dft_result for bad candidates, verify_dft_results_batch and reject_dft_results_batch to approve/reject multiple DFT results at once, approve_correction and reject_correction for single proposals, and approve_corrections_batch and reject_corrections_batch to bulk-approve/reject multiple corrections. Exports remain disabled unless the server export policy is explicitly enabled. "
            "Use read_paper_page to read a specific page when evidence is truncated or missing context. "
            "Inspect main-paper figures in the IDE workflow when stored captions or crops are insufficient. Figure review defaults to main paper only; do not automatically sweep all supplementary/SI figures unless include_supplementary_figures=true, the task explicitly cites Figure Sxx, or an evidence anchor points to an SI figure. "
            "Use recrop_figure to recalculate and persist an image crop. You can use 'full_page', 'wider', or 'ai_bbox' strategies. "
            "Figure image operations are direct-tool-only: when recropping an existing figure, you MUST call the MCP recrop_figure tool with the current figure_id and strategy='full_page'/'wider'/'ai_bbox'. Do not submit recrop_figure, bbox, or proposed_value={'bbox':...} through import_analysis/correction_proposals; the backend rejects that path. After calling recrop_figure, read back the figure and confirm image_path, crop_status, crop_source, and page. If you cannot access the MCP tool, report that you are blocked instead of saying the request was submitted. "
            "Use create_figure_from_bbox when a figure is missing entirely: read the PDF page, choose a bbox or full_page strategy, crop from the original PDF, and create the figure object directly. "
            "Use review_figure when the task is to record a figure verdict such as verified, needs_repair, or rejected; use import_analysis when the task is to correct figure_role, content_summary, key_elements, page, or caption metadata. Figure-derived DFT data must be submitted as DFT candidates/object_review_audits with figure/page/text/value/unit/property/material anchors, not as final verified or ML_Ready data. "
            "DFT audit AI may submit object_review_audits, issues, and correction candidates only. Dual-AI DFT consensus is recorded as an audit opinion and must not auto-verify, auto-reject, write human_verification, or move a DFTResult to ML_Ready. get_dft_audit_issues remains read_papers for both audit and primary AI, but only a primary repair AI key with repair_dft_issues may repair exactly one issue_id through repair_dft_audit_issue; audit AI, ordinary IDE AI, and propose-only keys must not call that repair tool. Repair output remains AI-applied candidate data pending later review, not human_verified or ML_Ready, and primary repair may mark needs_user_decision but may not mark false positive. Use verify_dft_result/reject_dft_result only after explicit human/user-authorized evidence review, never as an automatic audit-consensus step. "
            "Use GET /api/dft/audit-report for read-only DFT audit/repair health checks; it summarizes issue status/type counts, repair actor/capability counts, suspect repair warnings, and MCP capability lint warnings without exposing raw keys. "
            "Check mcp.capability_warnings in this agent guide; if repair_dft_issues appears on a non-primary-repair key, fix the key split before using repair_dft_audit_issue. "
            "Table object lifecycle is direct-tool-only: use update_table for caption/markdown/page/prov fixes, create_table for missing parsed tables, merge_table for split/continued/duplicate table fragments, and delete_table for invalid table objects. Do not only write a backend-request note, and do not submit table deletion/merge through import_analysis. Use the table object's real paper_id for table tools; SI table objects usually belong to the related SI paper_id even when their scientific evidence is used for the main paper. "
            "Use get_review_coverage only as a high-level coverage aid; for authoritative field readback, prefer get_paper or get_codex_item. "
            "Use get_field_disputes to find conflicting values proposed by different AIs. Includes historically resolved disputes (status='resolved') so later AIs know what was already settled. "
            "Use scan_duplicate_dois to find papers that share the same DOI, which may indicate duplicates in the system. "
            "Use create_share_token to generate a read-only share link for others to view papers, figures, DFT data, and audit logs without MCP access. "
            "IMPORTANT: The primary workflow does not depend on a backend-owned LLM. Even if auto_run_stage2_extraction is disabled, or backend writer/internal parser settings are missing, the system can still prepare AI-readable materials. In that mode, YOU (the AI) should first read the prepared workspace, codex context, item context, and evidence package, then continue analysis through MCP/import_analysis, notes, corrections, and review-safe candidate flows. The paper can remain in a material-ready state while waiting for IDE-AI follow-up. "
            "Overall parse review instruction: do not only write a report. For non-DFT text metadata, table field corrections, sections, and writing cards, directly write fixes back through import_analysis(auto_apply_review_rules=true); later AI writes may overwrite earlier AI writes. Table object lifecycle and figure image creation/recropping are direct MCP tool paths: use update_table/create_table/merge_table/delete_table for table objects and recrop_figure/create_figure_from_bbox for image files or bbox crop requests. DFT data must not be directly final-approved by audit AI; write object_review_audits or correction candidates only. DFT consensus remains an audit opinion and cannot write final truth or human_verification. "
            "IMPORTANT evidence_location format for auto-apply: object_review_audits.evidence_location and correction_proposals.evidence_payload should be structured dicts with at least one anchor key such as page, table, figure, quoted_text, section, bbox, or evidence_text. A plain string like \"PDF page 13, Table 5\" is accepted as quoted_text, but the preferred form is {\"page\": 13, \"table\": \"Table 5\", \"quoted_text\": \"...\"}. Structured keys enable reliable page-level evidence tracing; use the dict form whenever the exact page/figure/table is known. "
            "Figure review must cover every main-paper figure object in the requested scope, not only figures that already look correct. For each scientific main-paper figure, write or correct figure_role, content_summary, key_elements, page, caption, and crop_status/crop_quality; mark non-scientific images as figure_role='noise' or crop_status='noisy'. key_elements must be concrete visual/scientific elements such as materials, structures, curves, axes, orbitals, reaction steps, or panels; never use placeholders like verified_figure, ai_verified, reviewed, or ok. Check continuous figure numbering for the active scope, compare the paper's actual main-text figure/subfigure count in the PDF against current parsed figure objects, confirm whether any main-text figures are missing entirely, and check caption agreement with the PDF page, image_path readability, crop alignment, and whether create_figure_from_bbox, recrop_figure, review_figure, or another MCP/API tool is needed to create missing figures or repair bad crops. A figure without image_path, page, caption, figure_role, and content_summary is not RAG-ready; missing or placeholder key_elements must be corrected or clearly flagged as an analysis gap. Abstract/section review must check whether the abstract is missing, whether sections are crude Page 1/Page 2 splits, and whether normalized section_title and section_type should be created. Generate source_label dynamically, for example <agent_name>_overall_<YYYYMMDD_HHMMSS>; never use a fixed date. Do not overwrite English evidence fields with Chinese translation. Put Chinese only in *_zh derived fields where available, or in writing cards/review notes. "
            "Strict count and DFT safety update: never stop at a web UI display limit; compare the source PDF and context counts, and review all main-paper figure objects in scope one by one. SI figures are opt-in/anchor-triggered, while SI tables remain part of table/DFT evidence review. For DFT rows and figure-derived DFT candidates, verify material identity, property or energy type, value, unit, evidence text, source document type, and exact page/locator. Do not PASS or export DFT rows when material identity, review status, evidence text, or locator is missing; keep them as candidates behind the export safety gate. "
            "Use /api/intake/search for external candidate discovery, then approve and ingest candidates through the controlled intake endpoints. Do not use the legacy /api/papers/ai_workflow direct-ingest endpoint."
        ),
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "prompt_contract": prompt_contract(),
        "suggested_client_prompt": build_ide_review_prompt("overall"),
    }
