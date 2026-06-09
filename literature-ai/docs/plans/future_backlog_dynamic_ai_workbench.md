# Future Backlog: Dynamic AI Literature Workbench

## Purpose

This backlog records medium- and long-term improvements for the dynamic IDE AI workflow. It exists to prevent important review findings from being lost while keeping current execution focused.

These items are not immediate blockers for the current MCP dynamic AI workflow. Start them only after the active worktree is stabilized, tested, and committed in coherent groups.

## Current Baseline

- AI roles are assigned per task by the user.
- Codex, Gemini, GLM, Claude, or another IDE AI may parse, inspect figures, audit DFT rows, check writing cards, summarize knowledge, or perform second-pass review.
- AI output remains candidate evidence until review and confirmation gates promote it.
- Compatibility names such as `get_codex_context`, `GeminiAuditService`, `gemini_audit_protocol`, and `Gemini_Verified` do not imply fixed model ownership.

## 1. Evidence Location And Multimodal Parsing Reliability

### Why It Matters

DFT verification, image review, and writing-card trust all depend on being able to jump from a candidate value to the exact PDF evidence. Current parser output can still produce weak figure crops, missing bbox values, caption-only figures, or cross-page text/figure disconnects.

### Desired Capabilities

- Classify figure/table extraction reliability as usable, needs review, caption-only, invalid crop, or needs recrop.
- Link section mentions such as `Figure 4a` or `Table 2` to the actual figure/table object, even across pages.
- Improve `get_codex_item` so object-level review includes nearby text, figure/table captions, and related cross-references.
- Produce a per-paper artifact quality report that explains what evidence is directly usable and what must be manually checked.

### First Useful Deliverable

Create a read-only audit report for a sample of parsed papers:

- count figure crop statuses
- count missing/weak locators
- list candidates that rely on caption-only or text-only evidence
- show examples where section text references a figure/table that is not attached to the item context

Initial report contract:

- Endpoint: `GET /api/workbench/papers/{paper_id}/artifact-reliability`
- Optional aggregate endpoint: `GET /api/workbench/artifact-reliability`
- Schema version: `artifact_reliability_audit_v1`
- Paper summary fields: `figure_count`, `table_count`, `locator_count`, `figure_issue_counts`, `table_issue_counts`, `locator_issue_counts`, and `examples`.
- Example rows should include the object type, id, page, caption or evidence preview, status, and reason.
- Figure issues should quantify `missing_image`, `caption_only`, `small_crop`, `extreme_aspect_ratio`, `missing_bbox`, `missing_full_page_snapshot`, and `missing_page`.
- Locator issues should quantify `text_only_locator`, `missing_page`, `missing_locator`, `approximate_locator`, `unresolved_locator`, and missing bbox where applicable.

Boundary: this report is read-only. It must not recrop figures, repair locators, trust OCR or bbox automatically, mark candidates verified, approve corrections, or merge candidate values. It exists to quantify issue distribution before any repair workflow is designed.

### Not Yet

- Do not rebuild the PDF parser from scratch.
- Do not automatically trust OCR-derived coordinates without a review gate.

## 2. Multi-AI Review Conflict Aggregation

### Why It Matters

Dynamic AI assignment means multiple models may audit the same target. Disagreement is useful, but only if it is easy to compare. Without aggregation, conflicts become a pile of notes and corrections.

### Desired Capabilities

- Group reviews by `paper_id + target_type + target_id + field_name`.
- Show each reviewer's `source`, `source_label`, `agent_role`, `model_name`, decision, confidence, evidence location, and reason.
- Detect conflict types:
  - numeric value conflict
  - unit conflict
  - target/field mapping conflict
  - evidence locator conflict
  - accept vs reject decision conflict
- Preserve raw payload and normalized payload side by side.

### First Useful Deliverable

Add a read-only conflict summary API or report that lists unresolved conflicts without attempting automatic consensus.

### Not Yet

- Do not implement automatic majority voting or consensus promotion.
- Do not silently merge conflicting corrections.

## 3. Object-Level Review Payload Standardization

### Why It Matters

Paper-level external audit imports are now supported, but figure, table, DFT row, mechanism claim, and writing-card audits need a more uniform payload shape so different AI workers can write comparable results.

### Desired Schema

Recommended fields:

- `paper_id`
- `target_type`
- `target_id`
- `field_name`
- `decision`
- `evidence_checked`
- `evidence_location`
- `blocking_errors`
- `recommended_action`
- `corrected_value`
- `confidence`
- `source`
- `source_label`
- `agent_role`
- `model_name`
- `writes_final_truth`
- `human_confirmation_required`

### First Useful Deliverable

Document object-level review payload examples for:

- `dft_result`
- `figure`
- `table`
- `writing_card`
- `mechanism_claim`

Then add parser/validator support in `import_analysis` or a new non-breaking endpoint.

### Not Yet

- Do not require all external AI clients to migrate at once.
- Do not break existing paper-level `external_audit_opinion` imports.

## 4. Review UI And Evidence Jump Workflow

### Why It Matters

Strict gates are only practical if human review is fast. The reviewer should not manually hunt through PDFs for every candidate.

### Desired Workflow

- Left side: PDF/evidence viewer, opening directly to the relevant page.
- Right side: candidate value, parser evidence, AI opinions, conflicts, and actions.
- One-click actions:
  - approve
  - reject
  - needs fix
  - propose correction
  - open item context
- Support DFT rows, figures, tables, writing cards, and mechanism claims.
- Highlight weak locators, text-only evidence, missing bbox, and conflicting AI decisions.

### First Useful Deliverable

Improve the DFT review queue or review center so a row can open:

- source PDF page
- evidence text
- existing locators
- latest external audit opinions
- correction/review actions

### Not Yet

- Do not attempt a full annotation editor before page-level evidence jumping is reliable.
- Do not make UI approval bypass backend gates.

## 5. Downstream Feedback Loop

### Why It Matters

Exported datasets may be used in ML training, notebooks, or downstream scientific analysis. Errors found downstream should return to the source library as traceable corrections.

### Desired Capabilities

- Add a `submit_downstream_correction` style workflow.
- Record:
  - exported dataset/version
  - downstream task/source
  - affected paper/target/field
  - observed error
  - proposed correction
  - evidence or notebook reference
- Store feedback as pending correction or review candidate, not direct truth.
- Use feedback to improve extraction rules and prompts.

### First Useful Deliverable

Design the schema and API contract only. Keep implementation pending until export/versioning is stable.

### Not Yet

- Do not let downstream feedback directly mutate verified data.
- Do not add feedback UI before correction review UX is stable.

## 6. Compatibility Naming Migration

### Why It Matters

Historical names such as `GeminiAuditService`, `gemini_audit_protocol`, `Gemini_Verified`, and `get_codex_context` can mislead future AI workers into assuming fixed model roles.

### Desired Capabilities

- Add generic aliases while keeping old names working:
  - `/ai-audit` or `/external-ai-audit`
  - `ExternalAIAuditService`
  - `external_ai_audit_protocol`
  - generic display labels for `Gemini_*` workflow states
- Update UI copy to say "assigned AI review", "external AI reviewed", or "second AI review".
- Keep compatibility with old data, tests, and clients.

### First Useful Deliverable

Add generic UI labels and API aliases without renaming database enum/status values.

### Not Yet

- Do not rename workflow statuses in the database without a migration plan.
- Do not remove existing MCP/API names until all clients have migrated.

## Priority Recommendation

After the current worktree is stabilized, the next high-value sequence is:

1. Review UI and evidence jumping.
2. Multi-AI conflict aggregation.
3. Object-level review payload standardization.
4. Evidence location and multimodal parser reliability.
5. Compatibility naming aliases.
6. Downstream feedback loop.

The UI/evidence workflow should come first because the user is the orchestrator and final bottleneck. Faster review improves every downstream gate.
