# Literature AI MCP API

## Service URL

```text
http://localhost:8000/mcp
```

Transport:

- Streamable HTTP

Authentication:

- `Authorization: Bearer <MCP_API_KEY>`

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
LITAI_MCP_SERVER_NAME=Literature AI MCP
LITAI_MCP_API_KEYS=ide_ai|IDE AI|litmcp_ide_ai|read_papers,append_notes,propose_corrections,request_parse;admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections
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

## Capabilities

- `read_papers`: read paper metadata, parsed sections, candidates, evidence, Codex context, review coverage, and queues.
- `append_notes`: append non-final review notes.
- `propose_corrections`: propose corrections and import external analysis/audit candidates.
- `request_parse`: request local PDF scans, ingestion, or parsing.
- `review_corrections`: approve or reject pending correction proposals; reserve this for an admin or human reviewer.
- `review_dft`: optional narrower DFT review capability accepted by DFT verification tools.

## Dynamic AI Review Flow

Natural-language tasks such as "parse this paper", "audit DFT data", "check images", "check writing cards", "check mechanism claims", "check tables", or "import this external AI review" should be interpreted through [AI_TASK_ROUTING.md](./AI_TASK_ROUTING.md). Do not infer a fixed division of labor from model names. The user assigns the AI role per task, and `source`, `source_label`, `agent_role`, or `model_name` should record what happened in that run.

Use this flow when any IDE AI needs to review already parsed literature:

1. `query_papers` to find the paper.
2. `get_codex_context` for a compact paper bundle with artifact status, sections, figures, tables, structured candidates, evidence locators, warnings, and Markdown.
3. Optionally call `get_codex_item` for a low-token bundle for one section, figure, table, DFT result, mechanism claim, or writing card.
4. Optionally call `retrieve_evidence`, `read_paper_page`, `get_paper_knowledge`, `get_review_coverage`, or `get_field_disputes` for targeted checks.
5. Write the assigned AI's paper-level or object-level audit back through `import_analysis`.

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
- External audit imports must remain unverified until a human/final review step confirms them.
- Do not grant `review_corrections` to external AI clients unless that client is intentionally acting as a trusted admin.
- DFT export remains gated by safe verified evidence and exact locators.
