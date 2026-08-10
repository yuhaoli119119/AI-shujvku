# AI Task Routing

This document defines how natural-language user commands should be routed to MCP tools when the user dynamically assigns any IDE AI to a literature task.

The system does not bind task ownership to a model name. Codex, Gemini, GLM, Claude, or another IDE AI may perform any task below when the user assigns it. Historical names such as `get_codex_context`, `GeminiAuditService`, `gemini_audit_protocol`, `Codex_Candidate`, and `Gemini_Verified` are compatibility names only.

Ordinary AI output remains candidate or audit evidence. Only the dedicated, authenticated single-AI verification gate may promote a candidate to `ai_verified` after re-reading the original PDF, checking the exact locator, and passing the target-specific policy. Do not treat other AI output as final verified data.

## Common Routing Rules

- Start with `query_papers` unless the user already provided a `paper_id`.
- Use `get_codex_context` for the paper-level context bundle.
- For high-risk review, use `read_paper_page` to compare the parsed bundle with the original PDF before trusting parsed sections, tables, figures, or locators.
- Use `get_codex_item` for focused checks of a section, figure, table, DFT row, mechanism claim, writing card, or figure data point.
- Use `import_analysis` for paper-level AI audit opinions and other external AI review payloads.
- If the current IDE session does not expose MCP tools, use the repository-native backend path in `literature-ai/backend` and call `app.mcp.context.mcp_auth_context` plus `app.mcp.server` directly instead of stopping at tool-missing.
- Use `append_note` for non-final reviewer notes.
- Use `propose_correction` or `propose_dft_result_correction` for suggested data changes.
- For an untrusted direct `propose_correction`, acquire a module write lock when the target is top-level `abstract` or structured `sections`, `mechanism_claims`, or `writing_cards`. Other allowed metadata fields such as `title`, `year`, `journal`, and `authors` are not universally server-lock-enforced; table and figure tools follow their dedicated capability/evidence contracts.
- Use `release_module_write_lock` after the assigned lock-protected write task is complete.
- Route normal acceptance through `get_ai_verification_tasks` and `submit_ai_verification_batch` with a dedicated `ai_verify_content` key. Reserve legacy approve/reject tools for explicit exception handling.

Recommended capability set for ordinary IDE AI keys:

```text
read_papers,append_notes,propose_corrections,request_parse
```

Do not grant `review_corrections` to an ordinary IDE AI key unless that client is intentionally acting as a trusted admin.
Do not grant `repair_dft_issues` to ordinary IDE, DFT audit, or propose-only keys. Use a separate primary repair key with only `read_papers,repair_dft_issues` when the user explicitly assigns a DFT audit issue repair task.

## Single-AI Verification and Concurrency Rules

The safe LAN workflow is documented in [LAN_MULTI_AI_WORKFLOW.md](./LAN_MULTI_AI_WORKFLOW.md).

Content verification:

- Exactly one authenticated verifier handles a candidate through the `ai_verify_content` tools.
- Do not assign a second model, collect votes, compute AI consensus, or request third-AI adjudication for the same candidate.
- Multiple workers may run concurrently only when their paper/target sets do not overlap.
- Items that cannot pass automatically become `exception` and enter the human exception queue; ordinary accepted items do not wait for human review.

Non-DFT candidates and controlled writes:

- Sections, writing cards, figures, tables, notes, relationships, and paper metadata are unique paper objects.
- `import_analysis` retains ordinary non-DFT candidates under `authenticated_human_review_required` / `no_ai_overwrite`; it is not a direct-write route.
- Before an untrusted `propose_correction` directly applies a top-level `abstract` or structured `sections`, `mechanism_claims`, or `writing_cards` edit, acquire the relevant module write lock. Other allowed metadata fields are not universally server-lock-enforced.
- A candidate-only import with `auto_apply_review_rules=false` does not need a write lock.
- The identity using a lock-protected `propose_correction` should match the lock owner recorded by the server.

Module scopes (also available for optional operational coordination; listing a scope does not make every field server-lock-enforced):

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

Recommended non-overlapping split:

```text
Verifier: all acceptance decisions for the assigned paper/target set
Repair worker: candidate repair only; it cannot grant ai_verified
Other worker: a different paper or non-overlapping module
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

`read_papers` and `propose_corrections`. Ordinary DFT audit does not use final-review capabilities; authoritative AI acceptance requires `ai_verify_content`, while `review_corrections`/`review_dft` are reserved for the Owner-session exception path where applicable.
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
- This materialization is still not final verification or export approval. It must pass the dedicated single-AI verification gate.

DFT audit issue repair is a separate follow-up role:

- Required capability: `read_papers,repair_dft_issues`.
- Example key: `dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues`.
- The primary repair AI should first call `get_dft_audit_issues`, then call `repair_dft_audit_issue` for exactly one `issue_id` at a time.
- The audit AI, ordinary IDE AI, and propose-only keys must not call `repair_dft_audit_issue`.
- A repair result remains AI-applied candidate data, not human verification, safe verification, or ML/CSV export approval.

Expected verification order for high-risk DFT data:

1. The single verifier reads the original PDF and checks the exact locator, value, unit, entity/material binding, and unresolved conflicts.
2. It accepts, safely corrects and accepts, rejects, or marks the item `exception` in one bounded submission.
3. Only `exception` items are routed to a human; there is no second-AI vote or third-AI adjudication.

If a DFT row refers to a material or structure that has no `catalyst_sample`, create it through an authorized, evidence-backed `propose_correction`; `catalyst_samples` is not universally server-lock-enforced, though an operator may take an additional coordination lock. An ordinary `import_analysis` payload may only record the candidate need. Do not bind the DFT row to the paper's first sample. After the sample is created or unambiguously reused, submit its `catalyst_sample_id` field to the single-AI verification gate.

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
2. If the operator wants extra coordination around an authorized figure-metadata edit, it may call `acquire_module_write_lock` with `module_name="figures"`; this is not a universal server requirement for figure metadata.
3. `get_codex_context` with enough `max_figures`.
4. `read_paper_page` for the original PDF page that contains the figure or chart. This is mandatory before trusting figure crops or parser figure metadata.
5. `get_codex_item` with `item_type="figure"` for each figure needing inspection.
6. `append_note` for non-final visual caveats, `import_analysis` for candidate audit opinions, evidence-backed `propose_correction` for controlled metadata edits, or dedicated figure tools for image/review operations.
7. If an optional coordination lock was acquired, release it when the direct write task is complete.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_figure_audit`
- `source_label`: `Assigned AI image audit`
- `agent_role`: `figure_image_auditor`

Standard output or writeback:

Return inspected figure ids, crop/page/caption status, evidence notes, and proposed fixes. Use candidate notes, object-level audit candidates, or corrections; final figure trust remains a later review decision.

For controlled figure metadata correction, use evidence-backed `propose_correction`; the server does not universally require a figures lock for this field, though an operator may take one as additional coordination. Use `review_figure`, `recrop_figure`, or `create_figure_from_bbox` directly under their capability/evidence contracts for dedicated operations.

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
7. `append_note` or `import_analysis` for candidate review output; use `propose_correction` with a valid lock for an authorized writing-card edit.
8. `release_module_write_lock` when the direct write task is complete.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_writing_card_audit`
- `source_label`: `Assigned AI writing-card audit`
- `agent_role`: `writing_card_auditor`

Standard output or writeback:

Write review notes, candidate corrections, or an object-level `object_review_audits` entry with evidence examples and confidence. Keep citation support as draft/candidate unless evidence gates are satisfied later.

If the user explicitly allowed a non-DFT edit, use evidence-backed `propose_correction`. For untrusted direct writes, a valid module write lock token is server-required only for top-level `abstract` and structured `sections`, `mechanism_claims`, or `writing_cards`; broader locks are optional coordination. `import_analysis` remains a candidate/audit import.

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
6. Use `append_note` or `import_analysis` for candidate observations; use lock-protected `propose_correction` only when an actual mechanism-claim edit is authorized.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_mechanism_audit`
- `source_label`: `Assigned AI mechanism-claim audit`
- `agent_role`: `mechanism_claim_auditor`

Standard output or writeback:

Flag overclaims, missing qualifiers, unsupported causality, or conflicting evidence. Object-level audit payloads and proposed rewrites should remain pending candidates/corrections.

Forbidden:

Do not promote mechanistic interpretation to final truth without direct evidence and the authoritative object gate. `ai_verified` and human `verified` must remain distinct.

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
6. Use `update_table`, `create_table`, `merge_table`, or `delete_table` for table object mutations; use `import_analysis` only for paper-level or object-level table audit opinions. Use `propose_dft_result_correction` for DFT candidates derived from table evidence.

Required capability:

`read_papers`, `append_notes`, and `propose_corrections`.

Recommended provenance:

- `source`: `assigned_table_audit`
- `source_label`: `Assigned AI table audit`
- `agent_role`: `table_auditor`

Standard output or writeback:

Return table ids, row/column concerns, suspected missing candidates, and evidence-backed direct table-tool results or candidate opinions. Read back each mutated table.

Forbidden:

Do not apply table mutations through `import_analysis`. Do not create verified values from ambiguous table text.
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

Legacy multi-AI adjudication payloads are not an acceptance path. If prior opinions conflict, preserve them as evidence and route the target to `exception`; only a human exception decision or a later clean single-verifier run after the conflict is resolved may progress it.

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
      "confirmation_required": true
    }
  ]
}
```

Forbidden:

Do not import external AI output as final verified data. Do not bypass the artifact gate for paper-level audit. Do not overwrite prior audit opinions; preserve conflicts for the exception queue.
