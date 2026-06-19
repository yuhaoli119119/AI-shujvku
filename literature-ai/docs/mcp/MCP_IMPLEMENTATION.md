# Literature AI MCP Implementation Notes

## Current Role

The MCP layer lets external AI clients and IDE collaborators access the local literature workbench in a controlled way. It supports reading parsed literature, appending notes, proposing corrections, requesting parsing, importing external analysis, and exposing review queues.

MCP is a collaboration boundary, not a final-truth writer.

## Data Boundary

- PostgreSQL is the active business database.
- Parser artifacts live under the configured `LITAI_STORAGE_ROOT`.
- Docker Compose uses `LITAI_STORAGE_ROOT=/data/storage` with `./data:/data`.
- Local non-Docker runs must point `LITAI_STORAGE_ROOT` at the same real storage directory. From `literature-ai/backend`, use `../data/storage`.

If the storage root is wrong, paper rows may still be visible through PostgreSQL while artifact checks fail. Typical symptoms are `missing_pdf`, `missing_markdown_and_docling_json`, and `missing_ai_reading_package`.

## Authorization Model

HTTP MCP always uses Bearer tokens configured in `LITAI_MCP_API_KEYS`. Loopback, Docker bridge, and private-network addresses never receive anonymous capabilities. Repository-native calls made under `app.mcp.context.mcp_auth_context` remain available as the in-process IDE fallback and do not pass through HTTP authentication.

Each key has:

```text
source_prefix|display_name|raw_api_key|capability1,capability2
```

Capabilities are checked inside tool handlers:

- `read_papers` for read-only paper and evidence tools.
- `append_notes` for note creation.
- `propose_corrections` for correction proposals, AI review imports, and paper-level external audit candidates.
- `request_parse` for ingestion and parse requests.
- `review_corrections` for approving or rejecting corrections.
- `review_dft` as a narrower DFT review capability where accepted.
- `export_data` for Word/dataset export operations; this is also subject to the global `LITAI_EXPORTS_ENABLED` policy, which defaults to `false`.
- `create_share_links` for creating read-only share tokens; it is not implied by `read_papers` or review capabilities.

Recommended IDE AI capability set:

```text
read_papers,append_notes,propose_corrections,request_parse
```

This is enough for any IDE AI to read parsed context, request parsing, append notes, propose corrections, and import audit opinions. It is not enough to approve corrections or write final verified data.

## Dynamic AI Read And Audit Path

For parsed-paper review, the AI assigned to the current task should use:

- `query_papers`
- `get_codex_context`
- `get_codex_item`
- `retrieve_evidence`
- `read_paper_page`
- `get_paper_knowledge`
- `get_review_coverage`
- `get_field_disputes`
- `import_analysis`

For high-risk DFT, figure, chart, or table review, the intended order is:

1. Read `get_codex_context`.
2. Read the original PDF page through `read_paper_page`.
3. Compare parsed sections/tables/figures/locators against the original PDF.
4. Only then trust `get_codex_item`, `retrieve_evidence`, and parsed candidate structure for detailed review.

The parsed package is a candidate aid, not a substitute for checking the original PDF.

`get_codex_context` returns a compact paper bundle with:

- metadata and artifact status
- external audit precondition status
- sections, figures, tables, and Markdown
- structured candidates
- evidence locators
- DFT export readiness
- imported external analysis candidates
- warnings and recommended next actions

`import_analysis` can accept a paper-level audit payload from any assigned AI. When the payload has audit fields such as `verdict`, `recommended_action`, `suspected_missing`, or `evidence_examples`, the service creates an `external_audit_opinion` candidate.

`import_analysis` can also accept object-level review payloads through `raw_payload.object_review_audits`. For high-risk targets:

- Two ordinary AI reviews with evidence anchors may auto-materialize when they agree.
- For DFT rows that do not yet exist, `decision="new_candidate"` plus a structured `corrected_value` and `auto_apply_review_rules=true` will materialize an unverified `DFTResult` candidate and locator first; later review gates still apply.
- If the first two AI disagree, a third AI may adjudicate by submitting an object-level payload with:
  - `adjudication_role="third_ai"`
  - `adjudication_scope="conflict_resolution"`
  - `selected_source_ids=[...]`

That third-AI payload is still traceable candidate/audit data. It does not bypass evidence or audit logging.

The `source` and `source_label` fields record the role for that run, for example `glm_figure_audit`, `gemini_data_audit`, `codex_parse_review`, or `manual_second_pass`. The system does not hard-code a fixed job for Gemini, GLM, Codex, or any other AI.

## Artifact Gate

Paper-level external audit imports require the artifact gate to be ready:

- PDF exists and passes the basic quality/openability check.
- Markdown or Docling JSON has readable content.
- `by_id/<paper_id>/extraction/ai_reading_package.json` exists.
- Workflow status is not blocked for external audit.

When the gate fails, the import records `artifact_precondition_failed` instead of creating a trusted audit candidate. This prevents external AI from reviewing metadata-only or broken artifact records as if they were parsed papers.

## Safety Boundary

- External AI outputs are candidates.
- External AI should not trust parsed markdown, split tables, figure crops, or locators without checking the original PDF page first.
- External AI audit opinions are stored as `external_audit_opinion` candidates with `verification_status=unverified`.
- External AI does not automatically mark papers, fields, DFT rows, or citations as final verified truth.
- DFT export remains protected by review, evidence, and locator gates.
- `review_corrections` should remain reserved for trusted admin or human-review keys.

## Current Verification Snapshot

The current MCP/external-AI path was verified with:

- MCP and external analysis regression tests.
- Docker runtime artifact gate check against the active PostgreSQL database.
- A rollback probe that read a real paper via `get_codex_context` and simulated an `external_audit_opinion` import without committing.
