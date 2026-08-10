# Literature AI MCP Implementation Notes

## Current Role

The MCP layer lets external AI clients and IDE collaborators access the local literature workbench in a controlled way. It supports reading parsed literature, appending notes, proposing controlled corrections, requesting parsing, importing candidate analysis, exposing review queues, planning bounded multi-paper evidence, and running the dedicated AI verification path.

Ordinary MCP identities are collaboration/candidate writers, not final-truth writers. A separate server-authenticated identity with `ai_verify_content` may write `ai_verified` through the deterministic verification service; it can never write or impersonate human `verified`.

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
- `repair_dft_issues` for the primary DFT repair AI to call `repair_dft_audit_issue`; it is not implied by audit, proposal, or review capabilities.
- `ai_verify_content` for the one designated verifier to call `get_ai_verification_tasks`, `materialize_ai_section_page_fragments`, and `submit_ai_verification_batch`.
- `export_data` for Word/dataset export operations; this is also subject to the global `LITAI_EXPORTS_ENABLED` policy, which defaults to `false`.
- `create_share_links` for creating read-only share tokens; it is not implied by `read_papers` or review capabilities.

Recommended IDE AI capability set:

```text
read_papers,append_notes,propose_corrections,request_parse
```

This is enough for any IDE AI to read parsed context, request parsing, append notes, propose corrections, and import audit opinions. It is not enough to approve corrections or write final verified data.

Recommended DFT candidate, repair, verification, and exception roles:

```text
assigned_dft_audit|Assigned DFT Audit AI|<strong-random-key>|read_papers,propose_corrections
dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues
single_verifier|Single AI Verifier|<strong-random-key>|read_papers,ai_verify_content
human_reviewer|Human Reviewer|<strong-random-key>|read_papers,review_corrections,review_dft
```

The audit key may submit `object_review_audits`, issues, and correction candidates. Only the primary repair key should have `repair_dft_issues`; ordinary IDE, audit, propose-only, and human-review/admin examples should not receive that capability unless they are intentionally being used as the primary repair role.

Configuration lint diagnostics are available from `/api/system/agent-guide` as `mcp.capability_warnings` and from `/api/settings/ide-prompts` as `mcp_capability_warnings`. They warn when `repair_dft_issues` appears on a source/display name that is not a primary repair role. Diagnostics intentionally omit raw API keys.

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
- `plan_multi_paper_evidence`
- `get_ai_verification_tasks`
- `materialize_ai_section_page_fragments`
- `submit_ai_verification_batch`

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

- Ordinary object-review imports remain candidate evidence and cannot grant final acceptance.
- For DFT rows that do not yet exist, `decision="new_candidate"` plus a structured `corrected_value` and `auto_apply_review_rules=true` will materialize an unverified `DFTResult` candidate and locator first; the single-AI verification gate still applies.
- Conflicting ordinary opinions are not an acceptance path. The designated verifier must resolve them through deterministic evidence or submit `exception`; only that exception enters the Owner-session human queue.

The `source` and `source_label` fields record the role for that run, for example `assigned_figure_audit`, `assigned_data_audit`, `assigned_parse_review`, or `manual_review_import`. The system does not hard-code a fixed job for a product or model name.

## Authoritative AI Verification And Page Fragments

`get_ai_verification_tasks` is read-only and returns one stable limit/offset page, capped at 50. `submit_ai_verification_batch` accepts at most 20 submissions, defaults to `dry_run=true`, and reruns target fingerprint/version, PDF-page, evidence-text, exact-locator, numeric/unit, material-binding, and conflict checks. Formal passing decisions write `ai_verified`; exception decisions remain for Owner-session handling.

`materialize_ai_section_page_fragments` accepts at most 20 opaque fragment IDs/fingerprints, reruns recovery against the stored PDF, and rejects stale, approximate, forged, cross-paper, or cross-section inputs. Newly materialized objects remain unverified candidates and do not unlock the parent section.

`plan_multi_paper_evidence` is read-only. It plans bounded evidence in batches of at most 10 papers and preserves separate writing/citation eligibility. AI Writer uses the equivalent `/api/content-knowledge/writing-plan` endpoint and does not call `/api/writer/draft`.

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
- Ordinary external AI imports do not mark papers, fields, DFT rows, or citations as final verified truth.
- The dedicated `ai_verify_content` identity may write `ai_verified` only through `submit_ai_verification_batch`; it cannot write human `verified`.
- DFT export remains protected by review, evidence, and locator gates.
- `review_corrections` should remain reserved for trusted admin or human-review keys.
- `repair_dft_issues` should remain reserved for a separate primary DFT repair key, not ordinary audit/propose-only keys.
- Deployment changes should check the MCP capability lint warnings before running DFT issue repair.

## Verification Guidance

Validate the current checkout with focused MCP, external-analysis, single-AI verification, settings, and worker-startup tests. Treat historical test runs and production probes as dated evidence, not a permanent readiness conclusion.
