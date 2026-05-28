# D4-3G Read-only Proposal Manifest Runner / Human Confirmation Preparation Gate

Date: 2026-05-28

Scope: backend read-only runner plus generated proposal manifest for the four D4-3E YELLOW pending pilot review rows. This round did not write active DB rows, review rows, locators, materialized facts, registry entries, migrations, exports, or artifacts other than the reviewable docs/audits manifest.

## 1. Baseline / Sync

Required preflight commands:

- `git status --short`: clean
- `git log -1 --oneline`: `c02f6ae feat d4 locator recovery helper`
- `git rev-parse HEAD`: `c02f6aec3e74d84de6cc692ca152056103efdd4a`
- `git branch -vv`: `* master c02f6ae [origin/master] feat d4 locator recovery helper`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `c02f6aec3e74d84de6cc692ca152056103efdd4a refs/heads/master`

Conclusion: local `HEAD`, local `origin/master`, and remote `refs/heads/master` were identical at `c02f6aec3e74d84de6cc692ca152056103efdd4a` before D4-3G changes.

## 2. Modified Files

- `backend/app/services/locator_repair_manifest_runner.py`
- `backend/tests/test_d4_locator_repair_manifest_runner.py`
- `docs/audits/D4-3G_read_only_locator_repair_proposal_manifest.json`
- `docs/audits/D4-3G_read_only_proposal_manifest_runner.md`

No frontend files were modified.

## 3. Runner Behavior

`ReadOnlyLocatorRepairManifestRunner`:

- opens the active DB with SQLite URI `mode=ro`
- sets connection-local `PRAGMA query_only = ON`
- selects only the pilot paper `3978dc79f94f4457863fd68449ae293d`
- selects only the four D4-3E YELLOW review IDs for proposal generation
- explicitly excludes the `convergence_settings` RED review row
- calls the D4-3F `build_locator_repair_proposal()` helper
- uses target-specific evidence spans to avoid ambiguous `HAADF-STEM` locator matching
- narrows Docling candidates to blocks containing the selected target evidence span
- returns/writes a docs manifest only

The runner has no DB persistence path and does not import or call `save_reviews`, `mark_verified`, migration, extraction apply, materialize, or registry write code paths.

## 4. Generated Manifest

Generated file:

- `docs/audits/D4-3G_read_only_locator_repair_proposal_manifest.json`

Manifest safety defaults:

- `requires_human_confirmation=true`
- `should_write_locator=false`
- `mark_verified=false`
- `safe_verified=false`
- `export_eligible=false`
- `writing_eligible=false`

The manifest is only a human-review preparation artifact. It is not an automatic write plan and must not be treated as verified evidence.

## 5. Four YELLOW Proposal Results

| Review ID | Field | Value | Proposal status | Proposed page | Source artifact | Match method | Confidence | Blockers |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896` | `name` | `Fe-Co-V` | `yellow` | 7 | Docling `#/texts/80` | `target_token_match` | 0.56 | none |
| `09f83676-8f13-4e82-a576-ab359b264933` | `catalyst_type` | `single_atom` | `yellow` | 7 | Docling `#/texts/79` | `substring_match` | 0.68 | none |
| `280f2d9e-3ebb-4107-9702-f6ea6d645465` | `metal_centers` | `["Fe","Co","V"]` | `yellow` | 7 | Docling `#/texts/80` | `substring_match` | 0.68 | none |
| `56f72584-45b3-465b-9a40-97ec60a2fabf` | `rate` | `0.2C` | `yellow` | 6 | Docling `#/texts/74` | `substring_match` | 0.68 | none |

Common warnings:

- `proposal_not_verified`
- `does_not_unlock_export_or_writing`
- `target_specific_evidence_span_used_for_locator_query`
- `docling_candidates_narrowed_to_target_evidence_span`
- `current_review_evidence_text_not_used_as_locator_query`

Additional warning for `name`:

- `normalized_aggregate_value_not_literal_source_phrase`

## 6. RED Exclusion

Excluded row:

- review_id: `4ba0e490-5934-439c-8136-33a8ddf4e201`
- field: `convergence_settings`
- status: `RED`
- proposal: `none`
- reason: `no reliable source artifact / extracted empty-settings dict`
- `should_write_locator=false`
- `requires_human_confirmation=true`

This row remains blocked. No locator proposal was generated for it.

## 7. Test Coverage

New test file: `backend/tests/test_d4_locator_repair_manifest_runner.py`.

Coverage:

1. runner selects exactly four YELLOW rows
2. `convergence_settings` RED row is excluded
3. proposals default `should_write_locator=false`
4. proposals default `requires_human_confirmation=true`
5. proposals do not set `verified` and keep `safe_verified=false`
6. proposals keep `export_eligible=false` and `writing_eligible=false`
7. manifest includes required human-review fields
8. manifest records SQLite read-only mode and no DB/locator writes
9. runner does not write DB or modify review rows
10. missing artifact match emits blockers and does not fabricate page/bbox
11. ambiguous proposal remains yellow and not safe
12. missing page is not fabricated
13. missing bbox is not fabricated

Focused result:

- `py -m pytest tests/test_d4_locator_repair_manifest_runner.py`
- result: `12 passed`

Required backend verification:

- `py -m compileall app findpapers tests`
- result: passed, exit code 0
- `py -m pytest`
- result: `358 passed, 626 warnings in 252.02s`
- `git diff --check`
- result: passed, exit code 0

Frontend Playwright was not run because D4-3G did not modify frontend files.

## 8. Safety Confirmation

- active DB write: no
- locator write: no
- verified review write: no
- `mark_verified`: no
- `save_reviews`: no
- extraction/reprocessing apply: no
- materialize: no
- migration apply: no
- export/writing unlock: no
- artifact cleanup: no
- DB copy/move/delete: no
- registry write: no

Active DB read result during manifest generation:

- active DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- read mode: SQLite URI `mode=ro`
- pilot `evidence_locators` count observed by runner: 0

## 9. Remaining Risks

- The manifest is not human confirmation and cannot be used as a write authorization.
- The proposed locators are still proposals; D4-3H must be a separate explicit write gate.
- Current pending-row evidence text remains low-information or stitched for some rows, so the runner intentionally used target-specific evidence spans for matching.
- `name=Fe-Co-V` is a normalized aggregate rather than a literal source phrase.
- The RED `convergence_settings` row still needs a reliable source artifact before any future proposal is valid.
- Export/writing must remain blocked until controlled locator writes and separate human verification gates are satisfied.
