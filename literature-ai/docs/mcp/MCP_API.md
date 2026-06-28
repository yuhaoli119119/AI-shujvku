# Literature AI MCP API

## Service URL

```text
http://localhost:8000/mcp
```

Transport:

- Streamable HTTP

Authentication:

- `Authorization: Bearer <MCP_API_KEY>`
- Required for every HTTP MCP client, including localhost and private networks. `LITAI_MCP_ALLOW_UNAUTHENTICATED` is retained only as a deprecated configuration field and does not open HTTP MCP.

## Runtime Configuration

Docker Compose is the recommended runtime. In Docker, the backend uses:

```env
LITAI_STORAGE_ROOT=/data/storage
```

The compose file mounts `./data:/data`, so parsed PDFs, Markdown, Docling JSON, workspaces, and `ai_reading_package.json` are resolved from `literature-ai/data/storage`.

For local non-Docker runs, do not start the backend from `literature-ai/backend` with `LITAI_STORAGE_ROOT=./data/storage` unless you intentionally want `literature-ai/backend/data/storage`. Use one of these instead:

```env
# If running from literature-ai/backend
LITAI_STORAGE_ROOT=../data/storage

# Or use an absolute host path
LITAI_STORAGE_ROOT=D:/path/to/literature-ai/data/storage
```

If this root is wrong, MCP may still list papers from PostgreSQL, but artifact checks will report errors such as `missing_pdf`, `missing_markdown_and_docling_json`, or `missing_ai_reading_package`.

## Basic Environment Variables

```env
LITAI_MCP_ENABLED=true
LITAI_MCP_ALLOW_UNAUTHENTICATED=false
LITAI_MCP_SERVER_NAME=Literature AI MCP
LITAI_MCP_API_KEYS=ide_ai|IDE AI|litmcp_ide_ai|read_papers,append_notes,propose_corrections,request_parse;assigned_dft_audit|Assigned DFT Audit AI|litmcp_assigned_dft_audit|read_papers,propose_corrections;dft_primary_repair|DFT Primary Repair AI|litmcp_dft_primary_repair|read_papers,repair_dft_issues;human_reviewer|Human Reviewer|litmcp_human_reviewer|read_papers,review_corrections,review_dft
```

Single key format:

```text
source_prefix|display_name|raw_api_key|capability1,capability2
```

Recommended IDE AI key:

```text
ide_ai|IDE AI|<strong-random-key>|read_papers,append_notes,propose_corrections,request_parse
```

This lets any AI you run from the IDE read parsed paper context, request parsing, append notes, propose corrections, and import audit opinions as unverified candidates. It does not let that AI approve corrections or mark final verified data.

If you want audit logs to distinguish tools or agents, create multiple keys with the same safe capability set, for example `gemini|Gemini|...`, `glm|GLM|...`, or `codex|Codex|...`. This is optional; task assignment is controlled by your workflow, not by a fixed key name.

Recommended DFT audit and repair split:

```text
assigned_dft_audit|Assigned DFT Audit AI|<strong-random-key>|read_papers,propose_corrections
dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues
human_reviewer|Human Reviewer|<strong-random-key>|read_papers,review_corrections,review_dft
```

The DFT audit key may create issue/candidate evidence but must not receive `repair_dft_issues`. The primary repair key is intentionally narrow: it can read the DFT audit issue queue and call `repair_dft_audit_issue`, but it does not need proposal or final-review capabilities. Human/admin review keys can keep explicit verify/reject capabilities without implicitly becoming DFT issue repair keys.

## Capabilities

- `read_papers`: read paper metadata, parsed sections, candidates, evidence, Codex context, review coverage, and queues.
- `append_notes`: append non-final review notes.
- `propose_corrections`: propose corrections and import external analysis/audit candidates.
- `request_parse`: request local PDF scans, ingestion, or parsing.
- `review_corrections`: approve or reject pending correction proposals; reserve this for an admin or human reviewer.
- `review_dft`: optional narrower DFT review capability accepted by DFT verification tools.
- `repair_dft_issues`: permits `repair_dft_audit_issue` for the primary DFT repair AI only. Do not grant it to ordinary IDE, audit, or propose-only keys.
- `export_data`: permits Word/dataset exports only when `LITAI_EXPORTS_ENABLED=true`; `read_papers` no longer implies export.
- `create_share_links`: permits `create_share_token`; this is independent from read, export, and review capabilities.

## Dynamic AI Review Flow

Natural-language tasks such as "parse this paper", "audit DFT data", "check images", "check writing cards", "check mechanism claims", "check tables", or "import this external AI review" should be interpreted through [AI_TASK_ROUTING.md](./AI_TASK_ROUTING.md). Do not infer a fixed division of labor from model names. The user assigns the AI role per task, and `source`, `source_label`, `agent_role`, or `model_name` should record what happened in that run.

For LAN multi-computer workflows, see [LAN_MULTI_AI_WORKFLOW.md](./LAN_MULTI_AI_WORKFLOW.md). The short version is:

- DFT review opinions may be submitted concurrently by two or more AI clients.
- Non-DFT direct writes must first acquire a module write lock.
- Ordinary candidate-only imports can set `auto_apply_review_rules=false` and do not require a write lock.
- DFT `new_candidate` is the important exception: if you want missing DFT rows to enter the system's unverified DFT candidate queue automatically, send `decision=new_candidate` with a structured `corrected_value` and use `auto_apply_review_rules=true`. This materializes an unverified `DFTResult` candidate and still does not mark it exportable or final.
- Other computers should use MCP/API only; they should not directly modify the host file folder.

Use this flow when any IDE AI needs to review already parsed literature:

1. `query_papers` to find the paper.
2. `get_codex_context` for a compact paper bundle with artifact status, sections, figures, tables, structured candidates, evidence locators, warnings, and Markdown.
3. Read the original PDF or page-derived artifact with `read_paper_page` before trusting parsed sections, tables, figures, or locators for high-risk review.
4. Optionally call `get_codex_item` for a low-token bundle for one section, figure, table, DFT result, mechanism claim, or writing card.
5. Optionally call `retrieve_evidence`, `get_paper_knowledge`, `get_review_coverage`, or `get_field_disputes` for targeted checks.
6. Write the assigned AI's paper-level or object-level audit back through `import_analysis`.

For DFT rows, chart values, and figure/table-based claims, the expected behavior is:

1. One AI first compares the system-parsed materials with the original PDF and records parse/locator defects if found.
2. Two AI perform ordinary object-level review.
3. If the two AI disagree on a high-risk target, a third AI may adjudicate by reading the PDF and both prior opinions.

Paper-level audit payload example:

```json
{
  "paper_id": "PAPER_UUID",
  "source": "assigned_data_audit",
  "source_label": "Assigned AI data audit",
  "raw_payload": {
    "paper_id": "PAPER_UUID",
    "verdict": "WARN",
    "recommended_action": "needs_dft_review",
    "suspected_missing": ["dft_result"],
    "metadata_status": "ok",
    "section_structure_status": "ok",
    "table_status": "warn",
    "figure_status": "ok",
    "dft_status": "warn",
    "evidence_examples": [
      {"text": "DFT is discussed, but no directly verified DFT result is available."}
    ],
    "confidence": 0.7
  }
}
```

The `source` and `source_label` should describe the role you assigned for that run, such as `assigned_figure_audit`, `assigned_data_audit`, `assigned_parse_review`, or `manual_second_pass`.

The import creates an `external_audit_opinion` candidate with `verification_status=unverified`. It is visible in the review center, but it does not write final truth and does not unlock ML export.

## Module Write Locks

Direct AI writes for non-DFT modules are guarded by short-lived leases. A client must acquire a lock before calling `import_analysis` with `auto_apply_review_rules=true` when the payload can directly apply notes, corrections, relationships, figure/table changes, sections, or writing-card changes.

MCP tools:

```text
acquire_module_write_lock
release_module_write_lock
```

HTTP API:

```text
POST /api/module-locks/acquire
POST /api/module-locks/release
POST /api/module-locks/validate
GET  /api/module-locks
```

Supported module scopes:

```text
sections
writing_cards
figures
tables
content
metadata
notes
relationships
all_non_dft
```

Typical flow:

```text
acquire_module_write_lock(paper_id, module_name="content", locked_by="ai_pc_2")
import_analysis(..., reviewer="ai_pc_2", auto_apply_review_rules=true, write_lock_token="<token>")
release_module_write_lock("<token>")
```

If another AI already holds the same paper/module lock, acquisition fails with `module_write_lock_conflict`. If a direct write is attempted without a valid token, it fails with `module_write_lock_required`.

Object-level audit payload example:

```json
{
  "paper_id": "PAPER_UUID",
  "source": "assigned_dft_audit",
  "source_label": "Assigned AI DFT audit",
  "raw_payload": {
    "object_review_audits": [
      {
        "paper_id": "PAPER_UUID",
        "target_type": "dft_results",
        "target_id": "DFT_RESULT_UUID",
        "field_name": "value",
        "decision": "REVISE",
        "evidence_checked": true,
        "evidence_location": {"page": 7, "section": "Results", "table": "Table 1"},
        "blocking_errors": ["value_mismatch"],
        "recommended_action": "propose_correction",
        "corrected_value": -1.35,
        "confidence": 0.72,
        "source": "assigned_dft_audit",
        "source_label": "Assigned AI DFT audit",
        "agent_role": "dft_auditor",
        "model_name": "assigned-model",
        "reason": "The table reports -1.35 eV, not the extracted -1.20 eV.",
        "writes_final_truth": false,
        "human_confirmation_required": true
      }
    ]
  }
}
```

Object-level imports create `object_review_audit` candidates with `verification_status=unverified`. They are comparison evidence for queues and conflict aggregation. They do not approve corrections, merge values, mark extraction reviews verified, or unlock export.

### Creating a missing catalyst sample

Use `import_analysis` through the same external candidate and verification flow. A low-risk proposal may be represented as a correction candidate:

```json
{
  "correction_proposals": [
    {
      "field_name": "catalyst_samples",
      "target_path": "catalyst_samples:new:create",
      "operation": "create",
      "proposed_value": {
        "name": "Pt",
        "catalyst_type": "benchmark_comparator",
        "metal_centers": ["Pt"],
        "coordination": "Pt metal surface",
        "support": null,
        "synthesis_method": "commercial Pt catalyst comparator",
        "evidence_strength": "Original PDF exact-page text",
        "structure_name": "Pt catalyst"
      },
      "reason": "The PDF identifies a distinct Pt comparator.",
      "evidence_payload": {
        "page": 2,
        "section": "Introduction",
        "quoted_text": "0.44 eV on Pt"
      }
    }
  ]
}
```

For automated settlement, two AI lanes must instead submit matching `object_review_audits` with `target_type="catalyst_samples"`, `target_id="new"`, `field_name="create"`, and the same identity object in `corrected_value`. Missing PDF anchors remain `requires_resolution`. Before insertion, the backend compares normalized name, metal centers, coordination, support, and structure name within the same paper. One clear match is reused; multiple plausible matches remain unresolved and are never auto-merged.

Third-AI adjudication payload example:

```json
{
  "paper_id": "PAPER_UUID",
  "source": "assigned_third_ai_judge",
  "source_label": "Assigned AI conflict adjudication",
  "raw_payload": {
    "object_review_audits": [
      {
        "paper_id": "PAPER_UUID",
        "target_type": "dft_results",
        "target_id": "DFT_RESULT_UUID",
        "field_name": "value",
        "decision": "REVISE",
        "corrected_value": -1.26,
        "confidence": 0.92,
        "source": "assigned_third_ai_judge",
        "source_label": "Assigned AI conflict adjudication",
        "agent_role": "third_ai_judge",
        "model_name": "assigned-model",
        "reason": "After comparing the original PDF table and both prior AI opinions, -1.26 eV is the supported value.",
        "adjudication_role": "third_ai",
        "adjudication_scope": "conflict_resolution",
        "selected_source_ids": ["FIRST_AI_SOURCE_ID", "SECOND_AI_SOURCE_ID"],
        "normalized_energy_type": "adsorption_energy",
        "normalized_material": "Co-N-C host",
        "structure_name": "CoN4 single-atom site",
        "adsorbate": "Li2S6",
        "reaction_step": "adsorption",
        "evidence_checked": true,
        "evidence_location": {
          "page": 8,
          "section": "Results",
          "table": "Table 3",
          "quoted_text": "-1.26 eV"
        },
        "writes_final_truth": false,
        "human_confirmation_required": true
      }
    ]
  }
}
```

Semantics:

- If `adjudication_role` is omitted, the payload is treated as an ordinary review opinion.
- If `adjudication_role="third_ai"` and evidence anchors are present, the backend treats the payload as a third-AI adjudication candidate.
- The third AI must not guess; it must read the original PDF and preserve `selected_source_ids` so the adjudication remains traceable.

## Artifact Preconditions

`get_codex_context` returns:

```json
{
  "external_audit_precondition": {
    "status": "ready",
    "blocking_errors": []
  }
}
```

External paper-level audit imports are accepted only when the paper is ready for external audit. Required artifacts:

- PDF exists and is readable.
- Markdown or Docling JSON has content.
- `by_id/<paper_id>/extraction/ai_reading_package.json` exists.
- Workflow is not blocked as metadata-only, parse failed, or needs reingest.

If the status is `artifact_precondition_failed`, the assigned AI should not perform a final audit. Use the `blocking_errors` list to decide whether to reparse, repair paths, or attach the missing PDF.

## Common Tools

Reader and AI review tools:

- `query_papers`
- `get_paper`
- `get_codex_context`
- `get_codex_item`
- `get_paper_knowledge`
- `retrieve_evidence`
- `read_paper_page`
- `get_review_coverage`
- `get_field_disputes`
- `get_review_conflicts`
- `import_analysis`
- `append_note`
- `propose_correction`

Parsing tools:

- `scan_local_pdfs`
- `ingest_pdf_batch`
- `parse_paper`
- `get_parse_status`

Reviewer and admin tools:

- `get_correction_queue`
- `get_correction_detail`
- `approve_correction`
- `reject_correction`
- `verify_dft_result`
- `reject_dft_result`
- `propose_dft_result_correction`
- `get_dft_review_queue`

## Collaboration Rules

- External AI outputs are candidates, not verified facts.
- Ordinary parsed markdown, table splits, figure crops, and locators are not automatically trusted. High-risk review must compare them with the original PDF.
- External audit imports must remain unverified until a human/final review step confirms them.
- Do not grant `review_corrections` to external AI clients unless that client is intentionally acting as a trusted admin.
- Do not grant `repair_dft_issues` to external audit/propose-only clients. Use a separate `dft_primary_repair` key with `read_papers,repair_dft_issues`.
- DFT export remains gated by safe verified evidence and exact locators.
