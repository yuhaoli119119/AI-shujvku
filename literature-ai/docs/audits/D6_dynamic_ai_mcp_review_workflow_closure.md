# D6 Dynamic AI MCP Review Workflow Closure

**Date:** 2026-06-10
**Status:** Stage closure
**Baseline commit:** `a23ac06 feat: standardize object-level review audits`

This document records the current completed state of the dynamic AI / MCP review workflow. It is a closure and handoff note, not a replacement for `docs/current_baseline.md`.

## Scope

This stage covered the first usable loop for dynamically assigned AI review work:

- Dynamic AI role assignment: users may assign Codex, Gemini, GLM, Claude, or another IDE AI per task.
- MCP task routing: natural-language review tasks map to MCP tools, required capabilities, and safe writeback paths.
- DFT evidence queue: DFT rows expose evidence, review status, safety gates, conflict counts, and UI smoke coverage.
- External audit coverage: MCP and review surfaces expose unverified external audit coverage without granting final truth.
- Multi-AI conflict aggregation: opinions are grouped by `paper_id + target_type + target_id + field_name` and surfaced read-only.
- Object-level review audit payload: `import_analysis` accepts standardized object review payloads for DFT rows and other review targets.
- Object-level read-only review visibility now covers DFT rows, figures, writing cards, and mechanism claims.

## Key Commits

- `53d0ffd docs: align MCP workflow with dynamic AI role assignment`
- `13ac1f0 feat: surface external audit coverage in MCP`
- `26a21e4 fix: harden artifact path and active database handling`
- `9834ac7 feat: add intake screening workflow`
- `b771d21 fix: refine review and export workflow safeguards`
- `38e5c89 feat: improve DFT review evidence queue`
- `b1afecc test: cover DFT review evidence queue UI`
- `6af3725 feat: aggregate multi-ai review conflicts`
- `a23ac06 feat: standardize object-level review audits`
- `0bc10e1 feat: show object-level review audits in queues`
- `7d3c4f1 feat: show figure review audit summaries`
- `7e33730 feat: show writing card review audit summaries`
- `3fd71e4 feat: show mechanism claim review audit summaries`

## Current User-Facing Workflow

1. The user dynamically assigns an AI task, such as DFT audit, figure inspection, writing-card review, mechanism-claim check, intake screening, or second-pass review.
2. The assigned AI uses MCP to parse, read, retrieve evidence, inspect object context, and check current review state.
3. The AI writes back candidate evidence through safe MCP/API paths:
   - `append_note` for shared review notes.
   - `propose_correction` or `propose_dft_result_correction` for pending correction proposals.
   - `import_analysis` for paper-level `external_audit_opinion` or object-level `object_review_audit` candidates.
4. Object-level `object_review_audit` candidates are visible as read-only summaries across DFT rows, figures, writing cards, and mechanism claims.
5. Multiple AI or human reviewer opinions can be compared through review coverage and conflict aggregation.
6. Human review and final gates still control verified state, export eligibility, and citation readiness.

## Object Review UI Coverage

The read-only object review UI now covers:

- DFT rows in the evidence/review queue.
- Figure detail cards in the literature library.
- Writing-card detail cards in the literature library.
- Mechanism-claim detail cards in the literature library.

These surfaces show object audit counts, latest audit source/source label, decision, confidence, verification status, conflict counts, and target-specific evidence/safety/locator/confidence state where available. Conflict summaries are produced through the object-level conflict aggregation path and are displayed without attempting consensus.

This UI work did not add write interfaces. It does not automatically mark targets verified, approve correction proposals, materialize imported candidates, merge candidate values, or change DFT export and citation insertion gates.

## Safety Boundaries

- AI imports do not automatically mark extraction reviews or DFT rows as verified.
- AI imports do not automatically approve correction proposals.
- AI imports do not automatically merge candidate values into final target objects.
- DFT export remains blocked unless the existing evidence and review gates pass.
- Ordinary IDE AI keys should not receive `review_corrections`; that capability remains reserved for trusted admin or human-review use.
- `get_review_conflicts` is read-only and does not approve, merge, verify, or update review state.
- Object-level `object_review_audit` candidates are forced to `verification_status=unverified`, `writes_final_truth=false`, and `human_confirmation_required=true`.
- DFT queue and Review center object audit displays are read-only summaries; they do not automatically verify rows, approve corrections, or merge values.

## Verification Summary

Verification performed across this stage included:

- MCP, external analysis, schema, active DB, and settings regression coverage.
- Papers, library switching, intake, and DFT export workflow regression coverage.
- Docker-side MCP smoke checks for artifact and external audit readiness.
- Playwright smoke coverage for the DFT quality panel and review center external audit display.
- DFT evidence queue UI smoke coverage.
- Object-level audit tests covering:
  - DFT object review import.
  - Writing-card object review import.
  - Figure object review detail summaries.
  - Mechanism-claim object review detail summaries.
  - Object-level conflict aggregation.
  - MCP `import_analysis` object-level candidate summary.
- DFT queue and Review center UI smoke coverage for read-only `object_review_audit` display.
- Literature library Playwright smoke coverage for DFT, figure, writing-card, and mechanism-claim read-only summaries.
- Latest object-level audit validation: backend `49 passed`; Playwright smoke `30 passed` for the `mechanism|writing|literature library` slice.
- `py_compile` checks for changed backend modules.
- Inline script syntax checks for affected frontend pages where frontend scripts changed.
- `git diff --check` passed; only expected Windows line-ending warnings were observed during closure.

## Known Compatibility Names

The following names remain for compatibility and existing integration stability:

- `get_codex_context`
- `GeminiAuditService`
- `gemini_audit_protocol`
- `Gemini_Verified`
- `Codex_Candidate`

These names do not imply fixed model ownership. They are historical or public compatibility names. Current workflow semantics are role-based: the user assigns the AI role per task, and `source`, `source_label`, `agent_role`, and `model_name` record what happened in that run.

## Remaining Backlog

- Improve multimodal parsing, figure/table bbox reliability, crop status, and locator quality.
- Add a downstream feedback loop so reviewer outcomes can later inform prompts, routing, and quality metrics.
- Add generic API aliases for compatibility names where useful, while preserving existing public names.
- Build a richer conflict UI with filtering, target navigation, and evidence-location previews.
- Broaden object-level audit examples and UI rendering for table targets.

## Next Recommended Task

Recommended next work is the evidence-location and multimodal parsing reliability track:

- Add a read-only artifact, crop, and locator reliability audit report.
- Classify weak figure/table crops and evidence locators without rewriting the parser.
- Surface weak locator/crop reasons in review UI before adding any automatic repair workflow.

This next track should remain read-only first. Do not rebuild the PDF parser, automatically trust OCR-derived coordinates, automatically recrop figures, or promote candidates to verified state.
