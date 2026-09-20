"""Process-wide execution surface. Run once at startup, never per connection.

paper is the recommended default; full preserves callable legacy endpoints.
This module does not change authentication, permissions, or scientific gates.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from mcp.types import ToolAnnotations

EXCLUDED = {
    'review_paper': 'Disabled. Read evidence, then import_analysis; no backend LLM reviewer.',
    'verify_dft_result': 'Compatibility stub. Preflight submit(dry_run=true); formal apply_ai_verification_batch.',
    'reject_dft_result': 'Compatibility stub. Use apply_ai_verification_batch with evidence-backed decision.',
    'verify_dft_results_batch': 'Compatibility stub. Use apply_ai_verification_batch.',
    'reject_dft_results_batch': 'Compatibility stub. Use apply_ai_verification_batch.',
    'export_ml_dataset': 'Legacy v2 audit-writing export; use list_ml_export_tasks + export_paper_ml_dataset + read_ml_export_artifact.',
    'approve_correction': 'Use approve_corrections_batch(correction_ids=[id]); inspect per-item results.',
    'reject_correction': 'Use reject_corrections_batch(correction_ids=[id]); inspect per-item results.',
    'retrieve_evidence': 'Cross-paper semantic search, outside single-paper processing.',
    'plan_multi_paper_evidence': 'Cross-paper synthesis, outside single-paper processing.',
    'compare_papers': 'Cross-paper comparison, outside single-paper processing.',
    'scan_duplicate_dois': 'Cross-paper DOI maintenance, not within-paper DFT identity review.',
    'get_paper_knowledge': 'Writing-oriented knowledge view; get_codex_context/item provide processing context.',
    'create_share_token': 'Optional sharing, not required for authenticated artifact reading.',
    'cleanup_unused_figure_assets': 'Library maintenance, not a processing stage.',
}
CONTRACTS = json.loads(Path(__file__).with_name('tool_contracts.json').read_text(encoding='utf-8'))
PAPER_TOOLS = frozenset(CONTRACTS) - EXCLUDED.keys()
# Conservative for operations that overwrite/delete existing state; hints are not authorization.
DESTRUCTIVE = frozenset('repair_dft_audit_issue repair_dft_audit_issues_batch propose_correction update_table delete_table merge_table approve_correction approve_corrections_batch resolve_chart_review_actions finalize_chart_review apply_paper_review_batch recrop_figure review_figure cleanup_unused_figure_assets export_paper_ml_dataset'.split())
IDEMPOTENT_WRITES = frozenset({'apply_paper_review_batch','apply_ai_verification_batch','apply_paper_identity_rematerialization','finalize_ai_verified_dft_records'})
OPEN_WORLD = frozenset({'retrieve_evidence','ingest_pdf_batch','import_analysis','create_share_token'})

HANDOFFS = {
 'get_paper_review_task': 'Use this V2 task as the only source of object versions, task_fingerprint, prompt_version and standardized figure types.',
 'apply_paper_review_batch': 'Submit once per paper. Inspect all four outcome lists and authoritative_readback; only use get_paper_review_receipt after response loss.',
 'get_paper_review_receipt': 'Use only the original request_id after response loss; never change request_id to retry a different payload.',
 'search_figures': 'Matches whole-figure figure_types and compound-image panel_types; resolved_query_types explains alias resolution.',
 'query_papers': 'Use items[].id as paper_id. Keep all subsequent scientific evidence within that paper and its explicitly linked SI.',
 'scan_local_pdfs': 'folder_path is a server-visible allowlisted directory, not the AI client filesystem. Returns items[].path/paper_id/already_ingested.',
 'ingest_pdf_batch': 'Use a dedicated directory containing only the intended PDF(s). Returns results[].job_id/paper_id/status/error; get_parse_status accepts job_id. Check failed items before any retry.',
 'get_parse_status': 'job_id comes from ingest_pdf_batch.results[].job_id. Read status/error before proceeding to charts.',
 'get_chart_review_task': 'Read this before DFT extraction. run_id is optional; unresolved_actions are remaining work, not success.',
 'get_dft_review_task': 'Reads review work only; it does NOT execute DFT extraction. AI extracts from reviewed source evidence then imports candidates.',
 'import_analysis': 'Use structured raw_payload for deterministic mapping. Start from get_dft_review_task.import_analysis_template (also local_ai.import_analysis_template); retain server review_metadata, bundle fingerprint, completed chart snapshot, scope and coverage. Complete each audit and local evidence checks. For a genuinely new DFT candidate use target_id=new, target_type=dft_results, decision=new_candidate and evidence-backed corrected_value. The minimal fields alone are insufficient. Returned candidates[].id is an import candidate ID; candidates[].materialized_target_id is the DFT ID only when materialized_target_type=dft_results. Use handoff.dft_result_ids, never candidate IDs, in get_dft_review_task. On response loss, use get_analysis_import_status(paper_id, source_label=<unique label set before import>); do not blindly reimport.',
 'apply_analysis_review_rules': 'run_id comes from import_analysis or get_analysis_import_status. Inspect candidates[].materialized_target_id and handoff; pending/blocked is not a verified result.',
 'get_analysis_import_status': 'Read only your authenticated import runs for the exact paper. Prefer exact run_id; otherwise use the unique source_label chosen before import. Multiple matches are ambiguous: inspect run/candidate IDs, do not automatically select or reimport. Candidates use limit/offset; follow next_offset per run.',
 'get_ai_verification_record_tasks': 'Preferred bounded DFT verification reader. Copy returned IDs, versions, fingerprints and evidence locators; use next_cursor. Do not submit non-actionable blocked fields.',
 'get_ai_verification_tasks': 'Field-level reader for content targets; for mutating DFT batches prefer get_ai_verification_record_tasks keyset pagination to avoid offset skips.',
 'get_ai_verification_web_apply_package': 'Optional complete-paper package; for bounded responses use get_ai_verification_record_tasks. This is not an extraction tool.',
 'submit_ai_verification_batch': 'Recommended use is dry_run=true only. For formal writes use apply_ai_verification_batch with one stable request_id for the exact payload.',
 'apply_ai_verification_batch': 'Inspect items[].item_index/outcome/database_writes/errors and current_readback.items; HTTP/MCP success alone does not mean all items passed. If response is lost, query get_ai_verification_batch_receipt using original paper_id/request_id and same authenticated identity. Reuse the original request_id with identical submissions only; changed payload requires a deliberate new operation after resolving errors.',
 'get_ai_verification_batch_receipt': 'Returns per-item committed outcomes plus authoritative current_readback. Not-found is not proof a timed-out operation cannot still commit. Never change request_id to recover a lost response.',
 'review_paper_identity': 'Read-only derived identity/duplicate candidates, not scientific dedup completion. Identity apply is distinct from field-verification apply.',
 'get_paper_processing_status': 'Status is workflow guidance, not scientific approval. Default workflow uses the tools actually exposed by this process.',
 'list_ml_export_tasks': 'Use returned exact task key for export_paper_ml_dataset; never invent task from a display label.',
 'export_paper_ml_dataset': 'Use downloads.artifact.artifact_id and returned format metadata in read_ml_export_artifact. Excluded/blocked records remain excluded; export is not a review operation.',
 'read_ml_export_artifact': 'Use returned next_offset (byte offsets), not character count. Reassemble all blocks as documented and check byte_count/SHA-256 against artifact metadata; reading never regenerates artifacts.',
}

def configure_tool_surface(server, environ=None):
    env = os.environ if environ is None else environ
    profile = env.get('LITAI_MCP_TOOL_SET', 'paper')
    if profile not in {'paper','full'}:
        raise ValueError('Invalid LITAI_MCP_TOOL_SET; expected exactly paper or full (unset defaults to paper)')
    if getattr(server, '_litai_surface_configured', False):
        raise RuntimeError('Tool surface already configured; start a new process to change it')
    tools = server._tool_manager.list_tools()
    actual = {t.name for t in tools}
    if actual != set(CONTRACTS):
        raise RuntimeError(f'MCP contract inventory mismatch: missing={sorted(set(CONTRACTS)-actual)}, unclassified={sorted(actual-set(CONTRACTS))}')
    for tool in tools:
        contract = CONTRACTS[tool.name]
        tool.annotations = ToolAnnotations(
            readOnlyHint=contract['read_only'],
            destructiveHint=tool.name in DESTRUCTIVE,
            idempotentHint=contract['read_only'] or tool.name in IDEMPOTENT_WRITES,
            openWorldHint=tool.name in OPEN_WORLD,
        )
        suffix = f" Permission: {contract['capability']}. Side effects: {contract['effects']}"
        if tool.name in EXCLUDED:
            suffix += ' Compatibility/full only: ' + EXCLUDED[tool.name]
        suffix += ' ' + HANDOFFS.get(tool.name, '')
        if tool.name in {'review_paper', 'verify_dft_result', 'verify_dft_results_batch', 'reject_dft_result', 'reject_dft_results_batch'}:
            tool.description = EXCLUDED[tool.name]
        tool.description += suffix
    removed = sorted(EXCLUDED) if profile == 'paper' else []
    for name in removed:
        server.remove_tool(name)
    server._litai_surface_configured = True
    return {'profile':profile, 'scope':'process', 'removed':removed, 'tool_count':len(actual)-len(removed)}
