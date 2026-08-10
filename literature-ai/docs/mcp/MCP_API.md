# Literature AI MCP API

## Single-AI content verification

The default acceptance workflow is single-AI-first. A server-authenticated MCP identity with the dedicated `ai_verify_content` capability may use:

- `get_ai_verification_tasks`: read a bounded task list and current target fingerprints; this tool is read-only.
- `submit_ai_verification_batch`: submit a configured bounded batch (default 20, hard maximum 20). `dry_run=true` is zero-write; formal mode reruns deterministic PDF-page, evidence-text, exact-locator, target-snapshot, version, numeric/unit and unresolved-conflict checks before writing `ai_verified`, `rejected`, or `exception`.

`ai_verified` is not human `verified`. The audit payload records `actor_type=ai`, the authenticated source identity and label, model/agent identifier, capability, policy version, confidence, evidence and locator checks, target fingerprint, decision and time. Ordinary MCP keys, anonymous clients and request-body identity fields cannot create this status. The workflow never calls a second model and never uses AI consensus. Human Owner-session verification remains available only for the exception queue.

The Owner gateway does not inject `X-LitAI-Owner-Token`; browsers obtain the HttpOnly Owner session cookie through the explicit Owner-session login endpoint. MCP forwards the caller's own `Authorization` header without converting it to Owner identity.

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
LITAI_MCP_API_KEYS=ide_ai|IDE AI|<ide-key>|read_papers,append_notes,propose_corrections,request_parse;single_verifier|Single AI Verifier|<verifier-key>|read_papers,ai_verify_content;human_exception_reviewer|Human Exception Reviewer|<human-key>|read_papers,review_corrections,review_dft
LITAI_AI_VERIFICATION_MIN_CONFIDENCE=0.9
LITAI_AI_VERIFICATION_BATCH_LIMIT=20
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

Keep `ai_verify_content` on one designated verifier identity. Do not create a second verification lane, second-model review, voting, or consensus workflow.

Recommended DFT audit and repair split:

```text
assigned_dft_audit|Assigned DFT Audit AI|<strong-random-key>|read_papers,propose_corrections
dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues
single_verifier|Single AI Verifier|<strong-random-key>|read_papers,ai_verify_content
human_exception_reviewer|Human Exception Reviewer|<strong-random-key>|read_papers,review_corrections,review_dft
```

The DFT audit key may create issue/candidate evidence but must not receive `repair_dft_issues`. The primary repair key is intentionally narrow: it can read the DFT audit issue queue and call `repair_dft_audit_issue`, but it does not need proposal or final-review capabilities. Primary repair can mark `needs_user_decision`, but false-positive closure is reserved for an explicit human/admin action. Human/admin review keys can keep explicit verify/reject capabilities without implicitly becoming DFT issue repair keys.

Runtime diagnostics expose MCP capability lint warnings in `/api/system/agent-guide` under `mcp.capability_warnings` and in `/api/settings/ide-prompts` under `mcp_capability_warnings`. Check these warnings after editing `LITAI_MCP_API_KEYS`. A warning means `repair_dft_issues` appears on a key whose source/display name is not a primary repair role. The warning includes source/display/capability only and does not include the raw API key.

DFT audit/repair health can be checked with the read-only report endpoint:

```text
GET /api/dft/audit-report?paper_id=<optional-paper-uuid>&days=30&include_closed=false
```

The report groups DFT audit issues by status and issue type, groups `repair_dft_audit_issue` AuditLog rows by action and repair actor/capability, counts any `writes_final_truth=true` repair logs, returns suspect repair warnings, and includes the same MCP capability lint warnings. It does not modify DFT data and does not include raw API keys.

The DFT audit center UI is read-only: it is an issue queue, copy surface, and navigation entry to the paper DFT detail view. For issues bound to a real `dft_results` target, reviewers should open the DFT detail link and perform final human verify/reject there or through an equivalent explicit review tool. Issues with `target_id="new"`, missing targets, or source-scope errors must not show fake DFT detail links. Primary repair AI output remains pending review; it is not `human_verified`, `safe_verified`, or `ML_Ready`. Legacy AI adjudication and auto-advance controls are not valid DFT final-truth paths.

## Capabilities

- `read_papers`: read paper metadata, parsed sections, candidates, evidence, Codex context, review coverage, and queues.
- `append_notes`: append non-final review notes.
- `propose_corrections`: propose corrections and import external analysis/audit candidates.
- `request_parse`: request local PDF scans, ingestion, or parsing.
- `review_corrections`: approve or reject pending correction proposals; reserve this for an admin or human reviewer.
- `review_dft`: optional narrower DFT review capability accepted by DFT verification tools.
- `repair_dft_issues`: permits `repair_dft_audit_issue` for the primary DFT repair AI only. It does not permit false-positive closure. Do not grant it to ordinary IDE, audit, or propose-only keys.
- `ai_verify_content`: permits the dedicated authenticated single verifier to list verification tasks and submit bounded verification batches. It is distinct from candidate creation and human exception review.
- `export_data`: permits Word/dataset exports only when `LITAI_EXPORTS_ENABLED=true`; `read_papers` no longer implies export.
- `create_share_links`: permits `create_share_token`; this is independent from read, export, and review capabilities.

## Dynamic AI Review Flow

Natural-language tasks such as "parse this paper", "audit DFT data", "check images", "check writing cards", "check mechanism claims", "check tables", or "import this external AI review" should be interpreted through [AI_TASK_ROUTING.md](./AI_TASK_ROUTING.md). Do not infer a fixed division of labor from model names. The user assigns the AI role per task, and `source`, `source_label`, `agent_role`, or `model_name` should record what happened in that run.

For LAN multi-computer workflows, see [LAN_MULTI_AI_WORKFLOW.md](./LAN_MULTI_AI_WORKFLOW.md). The short version is:

- One dedicated verifier handles each candidate. Parallel clients must use non-overlapping paper/target sets; no second-AI vote, AI consensus, or third-AI adjudication is used.
- For untrusted direct `propose_correction` writes, the server requires a module lock only for top-level `abstract` and structured `sections`, `mechanism_claims`, and `writing_cards`. Other allowed metadata fields such as `title`, `year`, `journal`, and `authors` are not universally lock-enforced. Dedicated table/figure tools follow their capability/evidence contracts; `import_analysis` remains candidate-only for ordinary non-DFT output.
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

1. The dedicated single verifier re-reads the original PDF and checks the exact locator, current target version/fingerprint, value, unit, entity/material binding, and unresolved conflicts.
2. It submits one of `accept`, `correct`, `reject`, or `exception` through `submit_ai_verification_batch`.
3. Accepted and safely corrected items become `ai_verified`; rejected items remain blocked; only `exception` items require a human.

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

Short-lived leases protect the server-enforced subset of real non-DFT mutations. For an untrusted direct `propose_correction`, a lock is required only for top-level `abstract` and structured `sections`, `mechanism_claims`, and `writing_cards`. Other allowed fields such as `title`, `year`, `journal`, and `authors` are not universally server-lock-enforced. Use the capability and evidence contract of the dedicated table/figure tools for those object mutations. A lock does not convert ordinary `import_analysis` output into an overwrite: non-DFT imports remain `authenticated_human_review_required` under `no_ai_overwrite`.

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

Supported module scopes (some are also available for optional operational coordination; scope support does not mean every field is server-lock-enforced):

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

Typical controlled correction flow:

```text
acquire_module_write_lock(paper_id, module_name="content", locked_by="ai_pc_2")
propose_correction(..., write_lock_token="<token>")
release_module_write_lock("<token>")
```

If another writer already holds the same paper/module lock, acquisition fails with `module_write_lock_conflict`. If an untrusted direct correction in the enforced set (`abstract`, `sections`, `mechanism_claims`, or `writing_cards`) is attempted without a valid token, it fails with `module_write_lock_required`.

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
        "confirmation_required": true
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

Automated settlement uses the one designated `ai_verify_content` identity and `submit_ai_verification_batch`. Missing PDF anchors, ambiguous identities, or multiple plausible sample matches remain exceptions and are never auto-merged. No second or third AI lane exists.

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
- `get_ai_verification_tasks`
- `submit_ai_verification_batch`
- `materialize_ai_section_page_fragments`
- `plan_multi_paper_evidence`

`get_review_coverage` reports authoritative `content_object_gate` totals for
`sections`, `mechanism_claims`, and `writing_cards`: `total`,
`ai_verified`, `human_verified`, `exception`, `decision_recorded`/`reviewed`,
`unreviewed`, `authoritative_reviewed`, `can_use_for_writing`,
`can_use_for_citation`, `blocked`, and grouped `blocked_reasons`.
`decision_recorded` means that an active `ExtractionFieldReview` contains an
explicit AI/human/exception/rejection decision; therefore an `exception` is
reviewed but is not authoritative and remains blocked. `unreviewed` means no
such decision exists. The legacy section correction counter remains available
as `with_corrections` plus `correction_unreviewed`; it no longer overwrites the
decision-based `unreviewed` field. Section coverage also includes
`by_section_type`, so figure captions and body text cannot be conflated.

These totals are computed from canonical source rows, active field reviews, and
`content_object_gate`; `ContentEvidenceItem` is a non-authoritative search
projection. The Content Knowledge review-summary API and page display the same
coverage payload. Figure and table entries expose the
same eligibility keys but remain blocked with an explicit
`no_unified_content_object_gate` reason; their review/correction counters are
audit state only and must not be interpreted as writing or citation approval.

Cross-page body sections are not authorized by a local sentence match. Exact
single-page body evidence may be represented as `section_page_fragment`
`EvidenceClaim` objects bound to the unchanged parent Section. Each fragment
must have one 1-based physical PDF page, exact page text, its own locator and
field review, and its own `content_object_gate` result. Only verified fragments
may enter retrieval or writing plans; the parent and uncovered text remain
blocked.

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

`verify_dft_result`, `verify_dft_results_batch`, and the compatibility
`auto_finalize` option never create final verified state for an MCP identity.
They report the request as requiring `ai_verify_content`; the designated single
AI must use `submit_ai_verification_batch`. Owner-session review is reserved for
the exception queue.

## Collaboration Rules

- Ordinary external AI outputs are candidates. Only the authenticated single-AI verification service can promote a passing item to `ai_verified`.
- Ordinary parsed markdown, table splits, figure crops, and locators are not automatically trusted. High-risk review must compare them with the original PDF.
- External audit imports remain candidates until the unified single-AI service reruns all deterministic evidence gates.
- Do not grant `review_corrections` to external AI clients unless that client is intentionally acting as a trusted admin.
- Do not grant `repair_dft_issues` to external audit/propose-only clients. Use a separate `dft_primary_repair` key with `read_papers,repair_dft_issues`.
- Check `mcp.capability_warnings` / `mcp_capability_warnings` after deployment changes; fix any `repair_dft_issues_non_primary_repair_key` warning before running DFT issue repair.
- DFT export remains gated by safe verified evidence and exact locators.
- DFT final AI verify/reject must use `ai_verify_content`; human Owner-session decisions are only for unresolved exceptions. Neither path may impersonate the other.

### `materialize_ai_section_page_fragments`

Materializes deterministic Section page-fragment candidates as pending `EvidenceClaim` rows. The
tool requires the server-authenticated `ai_verify_content` MCP identity and accepts only
`paper_id`, `parent_section_id`, plus at most 20 `{fragment_id, fragment_fingerprint}` references.
It does not accept client-supplied page text or page numbers. The server reruns
`EvidencePageRecoveryService.recover_section_page_fragments()` against the stored PDF and rejects
stale, approximate, forged, cross-paper, or cross-section references before staging any write.

`dry_run` defaults to `true`; PostgreSQL is placed in a read-only transaction and the response has
`database_writes=false`. Formal materialization is atomic and idempotent, and creates only
`source_type=section_page_fragment`, `validation_status=unverified` candidates. It never creates an
AI/human verified review and never unlocks the parent Section. Materialized candidates can then be
read through `get_ai_verification_tasks(target_type="section_page_fragments")` and evaluated through
`submit_ai_verification_batch(dry_run=true)`.

### Multi-paper evidence planning and AI Writer

`plan_multi_paper_evidence` and `/api/content-knowledge/writing-plan` are bounded, read-only evidence planners. They accept at most 10 papers per batch, do not load all paper full text, and do not write review state. `content_object_gate` keeps `can_use_for_writing` separate from `can_use_for_citation`: writing-only evidence may appear in writing context but not citation plans, while blocked/unreviewed content appears in neither. AI Writer calls only `/api/content-knowledge/writing-plan`, never `/api/writer/draft`.
