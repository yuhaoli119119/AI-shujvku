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

## Current User-Facing Workflow

1. The user dynamically assigns an AI task, such as DFT audit, figure inspection, writing-card review, mechanism-claim check, intake screening, or second-pass review.
2. The assigned AI uses MCP to parse, read, retrieve evidence, inspect object context, and check current review state.
3. The AI writes back candidate evidence through safe MCP/API paths:
   - `append_note` for shared review notes.
   - `propose_correction` or `propose_dft_result_correction` for pending correction proposals.
   - `import_analysis` for paper-level `external_audit_opinion` or object-level `object_review_audit` candidates.
4. Multiple AI or human reviewer opinions can be compared through review coverage and conflict aggregation.
5. Human review and final gates still control verified state, export eligibility, and citation readiness.

## Safety Boundaries

- AI imports do not automatically mark extraction reviews or DFT rows as verified.
- AI imports do not automatically approve correction proposals.
- AI imports do not automatically merge candidate values into final target objects.
- DFT export remains blocked unless the existing evidence and review gates pass.
- Ordinary IDE AI keys should not receive `review_corrections`; that capability remains reserved for trusted admin or human-review use.
- `get_review_conflicts` is read-only and does not approve, merge, verify, or update review state.
- Object-level `object_review_audit` candidates are forced to `verification_status=unverified`, `writes_final_truth=false`, and `human_confirmation_required=true`.

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
  - Object-level conflict aggregation.
  - MCP `import_analysis` object-level candidate summary.
- Latest object-level audit validation: `45 passed`.
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

- Expand review UI and evidence jump workflows beyond DFT.
- Improve multimodal parsing, figure/table bbox reliability, crop status, and locator quality.
- Add a downstream feedback loop so reviewer outcomes can later inform prompts, routing, and quality metrics.
- Add generic API aliases for compatibility names where useful, while preserving existing public names.
- Build a richer conflict UI with filtering, target navigation, and evidence-location previews.
- Broaden object-level audit examples and UI rendering for figure, table, writing-card, and mechanism-claim targets.

## Next Recommended Task

Recommended next work is one of:

- Add a lightweight UI surface for `object_review_audit` candidates in the review center or target detail panels.
- Extend the DFT evidence jump experience to `figure`, `writing_card`, and `mechanism_claim` review targets.

Both options should remain read-only or candidate-only at first. Do not add automatic consensus, automatic merge, or automatic verification in the next step.
