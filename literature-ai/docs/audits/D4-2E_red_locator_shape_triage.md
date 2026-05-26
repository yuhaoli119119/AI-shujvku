# D4-2E RED Locator Shape Triage / Regression Lock Gate

Date: 2026-05-27

Scope: targeted triage of the active DB RED locator shape for paper `b234de0a6fff43f1aedb5f691f76004f`, plus backend regression locks. The active DB was opened read-only for inspection only. No real locator row, review row, extraction output, registry entry, materialized fact, DB file, or artifact was modified.

## 1. Baseline / Sync

Commands requested and executed before any triage/edit:

- `git status --short`: clean
- `git log -1 --oneline`: `2e68259 docs d4 active 15 papers extraction snapshot`
- `git rev-parse HEAD`: `2e682594d21077f5d94345e19df58ca35d27a013`
- `git branch -vv`: `* master 2e68259 [origin/master] docs d4 active 15 papers extraction snapshot`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `2e682594d21077f5d94345e19df58ca35d27a013 refs/heads/master`

Baseline conclusion:

- Starting HEAD, local `origin/master`, and remote `refs/heads/master` were identical at `2e682594d21077f5d94345e19df58ca35d27a013`.
- Worktree before edits: clean.

## 2. Active DB Confirmation

- Active registry path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- Resolved active DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `papers_total`: 15
- Active DB read mode for this gate: SQLite `mode=ro` raw queries only.

## 3. RED Row Read-only Facts

- Paper ID: `b234de0a6fff43f1aedb5f691f76004f`
- Title: `Revealing the 16-electron sulfur reduction reaction network in lithium sulfur (Li-S) batteries`
- DOI: `10.1360/TB-2024-0179`
- Locator row ID: `aeff6c86cd3e46f1a5a8ac262f209d00`
- Claim / evidence claim ID: none (`claim_id=NULL`; no `evidence_claims` rows for this paper)
- Related evidence span ID: `638e72e163564e45a2c22234dc722d01`
- Locator target: `target_type=catalyst_samples`, `target_id=7c58a29a-585e-4664-ad2b-d4334c81bd57`
- Materialized fact row: `catalyst_samples.id=7c58a29a585e4664ad2bd4334c81bd57`, `name=V`
- Other materialized row for paper: `dft_settings.id=914b7921b4c042de842ae608b1284364`
- External analysis candidate row: none
- `locator_status`: `text_only`
- Page raw value: SQL `NULL`
- Bbox raw value: JSON column contains string/text value `null`, not SQL `NULL` and not a usable bbox dict
- `bbox_json` / `coordinates` columns: not present in the active schema
- Warning reason: `page missing from parser output`
- Evidence text summary: short parsed text mentioning Sautet's team, N,S-HGF/HGF models, SRR conversion kinetics, and a Li2S6 concentration peak near 1.8 V.
- Part of the 13 materialized fact rows: yes, through the related `catalyst_samples` row; the paper has 2 materialized rows and the active DB has 13 total materialized fact rows.
- Appears in evidence cards / locator API: yes, as the single `evidence_locators` row for this paper.
- Appears in writing cards: the paper has one writing card, but the card evidence chain does not carry review keys or locator keys; it remains blocked.
- Safe for export/writing: no. The active DB has zero reviews, zero DFT result rows, and this locator is text-only with missing page.

## 4. Code Audit Answers

1. `locator_status=text_only` bbox exposure: API serialization normalizes locator degradation and sets `can_jump_to_pdf_page=false` and `can_highlight_in_pdf=false`. A diagnostic bbox may still be serialized if one exists, but it is explicitly not usable.
2. Missing page bbox use: missing page now fails the safe locator gate even if bbox-like data exists. It cannot enable PDF jump, highlight, DFT export, or writing use.
3. Detail page PDF jump: frontend detail rendering requires normalized status `exact_page`, `can_jump_to_pdf_page !== false`, and `page > 0`; text-only/missing-page rows render degraded text, not a jump button.
4. Writing evidence pack: fixed. Writing cards with locator payloads that are `text_only`, `missing_page`, non-jumpable, or page-less are blocked with `unsafe_locator` even if a verified-like payload is embedded.
5. DFT export: fixed. Export eligibility now requires safe verified review, required evidence text/reference, and an exact PDF-page locator. Text-only/missing-page evidence adds `unsafe_locator` and is not exported.
6. `review_safety`: still requires human `reviewer_status=verified`, safe target resolution, required evidence text, and required evidence reference. This gate now also enforces safe locator precision for export and locator-bearing writing chains.
7. RED nature: the active RED row is a real data shape anomaly (`text_only`, `page=NULL`, bbox column populated as text `null`). PDF/UI normalization was already safe. Export/writing gates had a locator-precision gap for future verified-like scenarios, so a minimal code fix was needed.

## 5. Tests Added / Updated

Added:

- `backend/tests/test_d4_locator_shape_regression_lock.py`

Locked behaviors:

- `text_only + no page + bbox-like payload` degrades to text evidence only.
- No page means no PDF jump even if bbox exists.
- Text-only locator does not expose a usable highlight bbox.
- Text-only / missing-page locator payload does not unlock writing.
- Text-only / missing-page locator does not unlock DFT export.
- Verified-like writing payload cannot bypass the safe locator gate.
- API serialization either preserves bbox as diagnostic data or marks it unusable with jump/highlight flags false.

Updated:

- `backend/tests/test_export_safety_gate.py`
- `backend/tests/test_writing_safety_gate.py`

Updates make previous positive fixtures explicit about page-backed evidence and lock missing-page export as blocked.

## 6. Code Fix

Minimal backend fix was needed in `backend/app/utils/review_safety.py`:

- Export gate now derives locator provenance from `evidence_locators`, falling back to page-bearing `evidence_spans` / `evidence_claims`.
- Export eligibility adds `unsafe_locator` when evidence exists but no exact PDF-page locator is available.
- Writing card safety detects nested locator payloads and blocks unsafe locator shapes with `unsafe_locator`.
- Raw locator payloads are not deleted or rewritten; only normalized safety views are stricter.

## 7. Verification

Targeted verification:

- `py -m pytest tests\test_d4_locator_shape_regression_lock.py tests\test_export_safety_gate.py tests\test_writing_safety_gate.py`
- Result: 20 passed.

Full required verification:

- `py -m compileall app findpapers tests`
- Result: passed.
- `py -m pytest`
- Result: 321 passed, 511 warnings.

Frontend Playwright: not run for this gate because no frontend files were modified and the existing detail page logic was read-only audited.

## 8. Safety Conclusion

The RED row can be downgraded to YELLOW after this guard because the anomaly is now locked as non-precise and cannot unlock PDF jump, bbox highlight, DFT export, or writing paths. It should remain YELLOW rather than GREEN until the underlying locator is repaired by a reviewed extraction/reprocessing pass that produces an exact page locator, followed by human verified reviews.

Next recommended gate: D4-2F reviewed extraction/reprocessing readiness for missing/weak locator coverage, still blocked from materialize/export/writing until exact locators and safe verified reviews exist.

## 9. Prohibited Actions Check

- Active DB write: no
- DB copy/move/delete: no
- Registry write: no
- Migration apply: no
- Verified review write: no
- Extraction/reprocessing apply: no
- Materialize: no
- Artifact cleanup: no
- Real data changed to reduce RED: no
