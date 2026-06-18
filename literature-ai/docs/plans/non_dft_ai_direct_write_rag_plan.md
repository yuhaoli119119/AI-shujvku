# Non-DFT AI Direct Write and RAG Completion Plan

## Objective

Let IDE AIs use the local MCP/API workflow without one-off human configuration, while keeping the only hard safety boundary on DFT truth export. Non-DFT content should be evidence-backed and directly writable by AI when module write locks and PDF anchors are present.

## Current Rules

- MCP runs at `/mcp/` and trusted local/private-network clients do not require a key by default.
- `import_analysis` with `auto_apply_review_rules=true` can auto-apply non-DFT outputs when a module write lock is supplied.
- DFT outputs are excluded from direct write:
  - `dft_results`
  - `dft_settings`
  - DFT export verification
- Figure crop/create is a direct MCP tool action, not an `import_analysis` correction:
  - `recrop_figure`
  - `create_figure_from_bbox`

## Direct-Write Non-DFT Modules

These modules are allowed for AI direct write when evidence and locks are valid:

- `metadata`
- `sections`
- `tables`
- `figures` metadata/captions/summaries
- `writing_cards`
- `mechanism_claims`
- `electrochemical_performance`
- `catalyst_samples`
- `notes`
- `relationships`

`content` and `all_non_dft` module locks cover the above content modules. `catalyst_samples` still require material anchors such as page, section, quoted text, table, or figure.

## Phase 1: Done in This Patch

- Expanded module write locks to cover `mechanism_claims`, `electrochemical_performance`, and `catalyst_samples`.
- Expanded ReviewService structured creation and validation for non-DFT modules.
- Allowed `auto_apply_review_rules=true` to apply evidence-backed non-DFT structured corrections.
- Kept DFT result/settings corrections out of the direct-write path.
- Expanded `ai_reading_package.json` with:
  - `non_dft_direct_write_policy`
  - `content_coverage`
  - `existing_structured_content`
  - task instructions for creating missing sections/writing cards and repairing non-DFT structured content.

## Phase 2: High-Quality RAG

Implemented or hardened retrieval cards for:

- Writing cards:
  - require evidence chain or safe AI-reviewed status before confirmed use.
  - include section strategy, figure logic, and paper-level claim positioning.
- Mechanism claims:
  - retrieve only claims with locator/evidence text or safe reviewed correction history.
  - expose claim type, evidence types, and supporting section/page.
- Electrochemical performance:
  - normalize capacity/rate/cycle/loading fields.
  - expose exact source text and table/figure anchors.
- Catalyst samples:
  - normalize material identity and aliases.
  - use material anchors before linking to mechanism/performance/DFT rows.

Current implementation adds a shared evidence card shape for non-DFT structured
RAG outputs: `source_type`, `source_id`, `paper_code`, `page`,
`evidence_text`, `review_status`, and `evidence_locator` when available.
`catalyst_samples` are now returned by the retriever and included in writing
evidence packs.

## Phase 3: Review and Manuscript Writing

Build a writing-oriented RAG layer that can answer:

- literature review synthesis by mechanism, material class, or performance metric.
- paper writing support for introduction, mechanism discussion, figure narrative, and conclusion.
- reference recommendation with constraints:
  - year window
  - journal name or journal family
  - impact factor threshold when metadata is available
  - topic relevance
  - evidence-backed claim fit

Word draft insertion must stay draft-safe:

- insert citations into a copy of the document.
- do not generate a final bibliography unless explicitly requested.
- preserve evidence links back to paper IDs and locators.

## Phase 4: 0006 MCP End-to-End Test

Use paper `0006` as the real workflow pilot:

1. Discover `A0006`/serial `0006` through the API or MCP context.
2. Read the Codex context and `ai_reading_package.json`.
3. Acquire a `content` or `all_non_dft` module write lock.
4. Use MCP/API to import a realistic non-DFT repair:
   - create or replace a writing card.
   - create or replace one mechanism/electrochemical/catalyst object when evidence exists.
5. Use direct figure tools only if an image crop is actually missing or wrong.
6. Confirm the database changed for non-DFT outputs.
7. Confirm DFT result/settings remain candidates and are not auto-verified.

## Test Coverage

- Unit/API tests should cover:
  - local keyless MCP prompt/config.
  - module lock coverage for new non-DFT modules.
  - non-DFT auto-apply with `auto_apply_review_rules=true`.
  - DFT auto-apply rejection.
  - AI reading package fields and direct-write policy.
- Real workflow tests should cover:
  - `0006` read context.
  - one successful non-DFT write.
  - one blocked DFT write or DFT candidate-only import.
