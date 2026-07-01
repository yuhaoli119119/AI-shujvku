# AI Task Routing

This document defines how natural-language user commands should be routed to MCP tools when the user dynamically assigns any IDE AI to a literature task.

The system does not bind task ownership to a model name. Codex, Gemini, GLM, Claude, or another IDE AI may perform any task below when the user assigns it. Historical names such as `get_codex_context`, `GeminiAuditService`, `gemini_audit_protocol`, `Codex_Candidate`, and `Gemini_Verified` are compatibility names only.

All AI output remains candidate or audit evidence until a later trusted review gate promotes it. Do not treat AI output as final verified data.

## Common Routing Rules

- Start with `query_papers` unless the user already provided a `paper_id`.
- Use `get_codex_context` for the paper-level context bundle.
- For high-risk review, use `read_paper_page` to compare the parsed bundle with the original PDF before trusting parsed sections, tables, figures, or locators.
- Use `get_codex_item` for focused checks of a section, figure, table, DFT row, mechanism claim, writing card, or figure data point.
- Use `import_analysis` for paper-level AI audit opinions and other external AI review payloads.
- If the current IDE session does not expose MCP tools, use the repository-native backend path in `literature-ai/backend` and call `app.mcp.context.mcp_auth_context` plus `app.mcp.server` directly instead of stopping at tool-missing.
- Use `append_note` for non-final reviewer notes.
- Use `propose_correction` or `propose_dft_result_correction` for suggested data changes.
- Use `acquire_module_write_lock` before directly applying non-DFT AI edits through `import_analysis(auto_apply_review_rules=true)`.
- Use `release_module_write_lock` after the assigned non-DFT write task is complete.
- Reserve `approve_correction`, `reject_correction`, `verify_dft_result`, and `reject_dft_result` for trusted admin or human-review keys.

Recommended capability set for ordinary IDE AI keys:

```text
read_papers,append_notes,propose_corrections,request_parse
```

Do not grant `review_corrections` to an ordinary IDE AI key unless that client is intentionally acting as a trusted admin.
Do not grant `repair_dft_issues` to ordinary IDE, DFT audit, or propose-only keys. Use a separate primary repair key with only `read_papers,repair_dft_issues` when the user explicitly assigns a DFT audit issue repair task.

## Multi-AI Concurrency Rules

The safe LAN workflow is documented in [LAN_MULTI_AI_WORKFLOW.md](./LAN_MULTI_AI_WORKFLOW.md).

DFT data:

- Two or more AI clients may review the same paper and the same DFT row concurrently.
- They should submit `object_review_audits` or DFT correction proposals as candidate evidence.
- Final DFT acceptance still requires the DFT consensus/adjudication/review gates.
- Do not use module write locks to serialize ordinary DFT review opinions.

Non-DFT direct writes:

- Sections, writing cards, figures, tables, notes, relationships, and paper metadata are unique paper objects.
- Before an AI directly applies non-DFT edits, it must acquire a module write lock.
- A candidate-only import with `auto_apply_review_rules=false` does not need a write lock.
- The `reviewer` passed to `import_analysis` should match the `locked_by` identity used for the lock.

Module scopes:

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

Recommended same-paper split:

```text
AI-1: DFT review opinions + figures lock when applying image/figure fixes
AI-2: content lock for sections and writing cards
AI-3: second DFT review lane or another paper's content lock
```

## Command: "通过 MCP 解析文章"

Meaning:

The user wants the assigned AI/client to locate or ingest a paper and start parsing through MCP.
If MCP tools are unavailable in the current IDE session, the assigned AI should switch to the repository-native backend path in `literature-ai/backend` and use the same parser/read/write functions through `app.mcp.context.mcp_auth_context` and `app.mcp.server`.

Recommended tool order:

1. `scan_local_pdfs` when the user gives a local folder.
2. `ingest_pdf_batch` for local PDF batches, or `parse_paper` for DOI/arXiv/provider-based parsing.
3. `get_parse_status` until the parse job finishes.
4. `query_papers` to find the resulting paper row.
5. `get_codex_context` to confirm parsed artifacts and candidate visibility.

Required capability:

`request_parse` for scan/ingest/parse, plus `read_papers` for status and context reads.

Recommended provenance:

- `source`: key source prefix, for example `ide_ai`, `glm`, or `codex`.
- `source_label`: `Assigned AI parser`
- `agent_role`: `paper_parser`

Standard output or writeback:

Return parse job status, `paper_id`, artifact precondition status, and blocking errors if any. Use `append_note` only for parse caveats that should remain visible to reviewers.

Forbidden:

Do not mark parsed output verified. Do not invent missing metadata when parser/provider evidence is absent.

## Command: "核验 DFT 数据"

Meaning:

The user wants the assigned AI to compare DFT candidates against paper evidence.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue the same workflow through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` with `has_dft_results=true` or the supplied `paper_id`.
2. `get_codex_context` to inspect artifact status and DFT export readiness.
3. `read_paper_page` for the original PDF page(s) that contain the candidate table, section, or figure. This is mandatory before trusting parser structure for high-risk review.
4. `get_dft_review_queue` for rows needing review.
5. `get_codex_item` with `item_type="dft_result"` for each target row.
6. `retrieve_evidence` for targeted evidence checks when needed.
7. `propose_dft_result_correction` for concrete field changes, or `import_analysis` for a paper-level or object-level DFT audit opinion.

Required capability:

`read_papers` and `propose_corrections`. Use `review_corrections` or `review_dft` only for trusted final reviewers.
Do not use `repair_dft_issues` for this audit role.

Recommended provenance:

- `source`: `assigned_dft_audit`, or a model-specific label such as `gemini_dft_audit` when useful for logs.
- `source_label`: `Assigned AI DFT audit`
- `agent_role`: `dft_auditor`

Standard output or writeback:

Write paper-level audit results through `import_analysis` as an `external_audit_opinion`, write row/field checks through `import_analysis.raw_payload.object_review_audits`, or create pending row corrections through `propose_dft_result_correction`. The audit candidate must remain `verification_status=unverified`.

Stable missing-row workflow:

- For any paper, if the assigned AI finds a missing DFT row that should enter the system queue, submit it as `decision="new_candidate"` with `target_type="dft_results"`, `target_id="new"`, `field_name="dft_results"`, and a complete structured `corrected_value`.
- In that case, do not leave the import as candidate-only. Call `import_analysis` with `auto_apply_review_rules=true` so the backend materializes an unverified `DFTResult` candidate plus locator.
- This materialization is still not final verification, not export approval, and not a bypass of the later consensus/adjudication gate.

DFT audit issue repair is a separate follow-up role:

- Required capability: `read_papers,repair_dft_issues`.
- Example key: `dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues`.
- The primary repair AI should first call `get_dft_audit_issues`, then call `repair_dft_audit_issue` for exactly one `issue_id` at a time.
- The audit AI, ordinary IDE AI, and propose-only keys must not call `repair_dft_audit_issue`.
- A repair result remains AI-applied candidate data, not human verification, safe verification, or ML/CSV export approval.

Expected review order for high-risk DFT data:

1. One AI first compares system parsing against the original PDF and records parse defects, locator defects, or table/figure split defects.
2. Two AI then perform ordinary DFT review.
3. If those two AI disagree, a third AI may adjudicate after reading the original PDF and both prior AI outputs.

If a DFT row refers to a material or structure that has no `catalyst_sample`, first use `import_analysis` to propose or dual-review `catalyst_samples:new:create` with an original-PDF anchor. Do not bind the DFT row to the paper's first sample. After the sample is created or unambiguously reused, submit the normal dual-AI `catalyst_sample_id` review for the DFT row.

DFT page-locator boundary:

- A DFT row can be reviewable with paper provenance, source section, and evidence text even when the exact PDF page is missing.
- If the current parsed artifacts do not contain a unique exact evidence-text-to-page match, keep the row as `text_only` / missing-page evidence. Do not infer a page from similarity alone.
- The web UI should not expose an "AI find PDF page" action unless a real backend workflow exists. In the current workflow, ad hoc page investigation is performed by the assigned IDE AI when the user requests it, and any result must remain a candidate until reviewed.
- Do not use page-recovery work to mark DFT rows verified, approve corrections, bind materials, or unlock ML/CSV export.

Forbidden:

Do not call final verification tools with an ordinary IDE AI key. Do not unlock ML export from external AI review alone. Do not infer precise numeric values from plots unless the value is explicitly readable in source evidence.
Do not trust parsed markdown or split tables without checking the original PDF page first.

## Command: "核验图片"

Meaning:

The user wants the assigned AI to inspect figure crops, captions, figure roles, image-derived claims, or visual evidence quality.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` or use the provided `paper_id`.
2. If the assigned AI will directly apply figure fixes, call `acquire_module_write_lock` with `module_name="figures"` before writing.
3. `get_codex_context` with enough `max_figures`.
4. `read_paper_page` for the original PDF page that contains the figure or chart. This is mandatory before trusting figure crops or parser figure metadata.
5. `get_codex_item` with `item_type="figure"` for each figure needing inspection.
6. `append_note` for non-final visual caveats, `propose_correction` for candidate fixes, or `import_analysis` with `write_lock_token` for direct non-DFT application.
7. `release_module_write_lock` when the direct write task is complete.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_figure_audit`
- `source_label`: `Assigned AI image audit`
- `agent_role`: `figure_image_auditor`

Standard output or writeback:

Return inspected figure ids, crop/page/caption status, evidence notes, and proposed fixes. Use candidate notes, object-level audit candidates, or corrections; final figure trust remains a later review decision.

For direct figure metadata or crop-status application, `import_analysis` must include `write_lock_token`.

Forbidden:

Do not treat figure crops as exact evidence without checking page/caption context. Do not estimate hidden or unreadable values from image trends.
Do not let parsed figure crops replace the original PDF page review.

## Command: "核验写作卡"

Meaning:

The user wants the assigned AI to check writing cards, knowledge candidates, and citation-support safety.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` with `has_writing_cards=true` or the supplied `paper_id`.
2. If the assigned AI will directly apply writing-card or section fixes, call `acquire_module_write_lock` with `module_name="content"` or `module_name="writing_cards"`.
3. `get_codex_context` to inspect writing cards and knowledge candidates.
4. `get_paper_knowledge` for mechanism, gap, method, and writing logic candidates.
5. `get_codex_item` with `item_type="writing_card"` for focused checks.
6. `retrieve_evidence` for source-backed support.
7. `append_note`, `propose_correction`, or `import_analysis` for review output. Direct non-DFT application requires `write_lock_token`.
8. `release_module_write_lock` when the direct write task is complete.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_writing_card_audit`
- `source_label`: `Assigned AI writing-card audit`
- `agent_role`: `writing_card_auditor`

Standard output or writeback:

Write review notes, candidate corrections, or an object-level `object_review_audits` entry with evidence examples and confidence. Keep citation support as draft/candidate unless evidence gates are satisfied later.

If the user explicitly allowed non-DFT direct editing, `import_analysis(auto_apply_review_rules=true)` may apply eligible writing-card or section corrections only when a valid module write lock token is supplied.

Forbidden:

Do not generate final bibliography or final citation-ready claims from unverified writing cards.

## Command: "核验机制 claim"

Meaning:

The user wants mechanism claims checked against source sections, figures, tables, and evidence locators.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` or use the provided `paper_id`.
2. `get_codex_context` to inspect mechanism candidates.
3. `get_paper_knowledge` with mechanism-oriented categories when useful.
4. `get_codex_item` with `item_type="mechanism_claim"`.
5. `retrieve_evidence` or `read_paper_page` for support checks.
6. `append_note`, `propose_correction`, or `import_analysis`.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_mechanism_audit`
- `source_label`: `Assigned AI mechanism-claim audit`
- `agent_role`: `mechanism_claim_auditor`

Standard output or writeback:

Flag overclaims, missing qualifiers, unsupported causality, or conflicting evidence. Object-level audit payloads and proposed rewrites should remain pending candidates/corrections.

Forbidden:

Do not promote mechanistic interpretation to final truth without direct evidence or human/final confirmation.

## Command: "核验表格"

Meaning:

The user wants extracted tables, table captions, and table-derived candidates checked against the paper.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` or use the provided `paper_id`.
2. `get_codex_context` with enough `max_tables`.
3. `read_paper_page` for the original PDF page that contains the table. This is mandatory before trusting parser table split or cell alignment.
4. `get_codex_item` with `item_type="table"` for each target table.
5. `retrieve_evidence` for table-derived structured rows.
6. `propose_correction` or `propose_dft_result_correction` for concrete fixes; `import_analysis` for paper-level or object-level table audit.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_table_audit`
- `source_label`: `Assigned AI table audit`
- `agent_role`: `table_auditor`

Standard output or writeback:

Return table ids, row/column concerns, suspected missing candidates, and proposed fixes. Use candidate writes only.

Forbidden:

Do not apply table-derived corrections directly. Do not create verified values from ambiguous table text.
Do not trust parsed table structure without checking the original PDF page first.

## Command: "导入外部 AI 审核意见"

Meaning:

The user already has an AI review result from another chat, IDE, model, or script and wants it stored in the workbench.
If MCP tools are unavailable in the current IDE session, use the repository-native backend path in `literature-ai/backend` and continue through `app.mcp.context.mcp_auth_context` plus `app.mcp.server`.

Recommended tool order:

1. `query_papers` or use the supplied `paper_id`.
2. `get_codex_context` to confirm artifact precondition status.
3. `import_analysis` with `raw_text` or `raw_payload`.
4. `get_review_coverage` or `get_codex_context` again to confirm the imported candidate is visible.

Required capability:

`read_papers` and `propose_corrections`.

Recommended provenance:

- `source`: role-specific, for example `external_ai_audit`, `glm_figure_audit`, `gemini_dft_audit`, or `manual_second_pass`.
- `source_label`: human-readable task label.
- `agent_role`: the assigned role, for example `external_audit_importer`.

Standard output or writeback:

`import_analysis` should create candidate records. Paper-level audit payloads should create `external_audit_opinion` candidates with `verification_status=unverified`. Object-level payloads should create `object_review_audit` candidates with the same unverified safety boundary.

When the task is third-AI adjudication instead of ordinary review, the object-level payload must include:

- `adjudication_role: "third_ai"`
- `adjudication_scope: "conflict_resolution"`
- `selected_source_ids: ["FIRST_AI_SOURCE_ID", "SECOND_AI_SOURCE_ID"]`

In that mode the AI is not doing a generic third extraction pass. It is explicitly deciding between prior opinions after checking the original PDF.

Object-level payload example:

```json
{
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
```

Forbidden:

Do not import external AI output as final verified data. Do not bypass the artifact gate for paper-level audit. Do not overwrite prior audit opinions; preserve conflicts for later review.
Do not submit third-AI adjudication without checking the original PDF and preserving `selected_source_ids`.
