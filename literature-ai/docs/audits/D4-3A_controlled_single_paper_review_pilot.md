# D4-3A Controlled Single-Paper Extraction-to-Review Pilot

Date: 2026-05-27

Scope: design and regression-lock a single-paper pilot path from existing extraction/materialized output into human review preparation. This pass did not run active-library extraction, reprocessing, materialization, mark-verified, export, writing, migrations, registry writes, DB file operations, or artifact cleanup.

## 1. Baseline / Sync

Commands requested and executed before DB triage or edits:

- `git status --short`: clean
- `git log -1 --oneline`: `5c6daf1 fix d4 locator shape normalization`
- `git rev-parse HEAD`: `5c6daf1af6d1e38e688b3730dc93b17e0f9cb3f2`
- `git branch -vv`: `* master 5c6daf1 [origin/master] fix d4 locator shape normalization`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `5c6daf1af6d1e38e688b3730dc93b17e0f9cb3f2 refs/heads/master`

Baseline conclusion:

- Starting HEAD, local `origin/master`, and remote `refs/heads/master` were identical at `5c6daf1af6d1e38e688b3730dc93b17e0f9cb3f2`.
- Worktree before edits: clean.

## 2. Active DB Confirmation

- Active registry path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- Resolved active DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- Active DB read mode: SQLite `mode=ro` raw queries only.
- `papers_total`: 15
- `extraction_field_reviews`: 0
- Safe verified reviews: 0

## 3. Selected Pilot Paper

Selected:

- Paper ID: `3978dc79f94f4457863fd68449ae293d`
- Title: `锂硫电池非均相电催化剂`
- DOI: `10.1360/TB-2022-0680`
- Year / journal: 2022, `Chinese Science Bulletin (Chinese Version)`

Why selected:

- Has PDF: yes.
- Has parsed text / markdown / docling / TEI artifacts: yes.
- Has materialized extraction output: 3 rows.
- Has evidence text: 11 `evidence_spans`, including readable catalyst and electrochemical snippets.
- Has current review rows: no.
- Has current safe verified review: no.
- Locator risk is lower than the former RED paper because it has no bbox-like inconsistent `evidence_locators` row.
- It is still not safe evidence: all evidence spans are missing page and no exact locator exists. The pilot is therefore strictly for pending human review preparation, not export/writing unlock.

Current state:

- `catalyst_samples`: 1 (`Fe-Co-V`, `single_atom`, metals `Fe`, `Co`, `V`, evidence strength `HAADF-STEM`)
- `dft_settings`: 1 reproducibility skeleton, many settings missing
- `electrochemical_performance`: 1 row, `rate=0.2C`, evidence text from cycling-performance context
- `dft_results`, `mechanism_claims`, `figure_data_points`: 0
- `evidence_spans`: 11, all `page=NULL`
- `evidence_locators`: 0
- `evidence_claims`: 0
- `writing_cards`: 1, blocked
- `extraction_field_reviews`: 0

Why other candidates were not selected:

- `7eecdb29ba60413fbeca82dc8532dd1e`: has PDF/text/extraction and two spans, but fewer facts/spans and less useful evidence text.
- `584bda44ec114811974d61bdb83a1fc7`: has PDF/text/extraction but no evidence rows.
- `729f4dc3cb3c45ca94c306e29eeebe80`: has PDF/text/extraction and two spans, but fewer facts/spans.
- `a2306ee3c87548f19b032181fc9bfa90`: has PDF/text/extraction but no evidence rows.
- `b234de0a6fff43f1aedb5f691f76004f`: former RED locator-shape paper; now guarded but intentionally avoided for the first controlled pilot.

## 4. Extraction-to-Review Chain Audit

Where extraction output lives:

- Materialized facts live in typed tables such as `catalyst_samples`, `dft_settings`, `dft_results`, `mechanism_claims`, and `electrochemical_performance`.
- `ExtractionSchemaService.result_payload()` serializes those materialized facts into schema-shaped extraction results.
- Evidence spans live in `evidence_spans`; evidence locators live in `evidence_locators`.
- `ExtractionSchemaService._with_reviews()` attaches review rows and resolved locators to result fields.

Review endpoints before this gate:

- `GET /api/extraction/results/{paper_id}` lists extraction result fields with any existing reviews and locator warnings.
- `GET /api/extraction/results/{paper_id}/reviews` lists persisted review rows.
- `POST /api/extraction/results/{paper_id}/reviews/save` saves manual review edits but rejects `reviewer_status=verified`.
- `POST /api/extraction/results/{paper_id}/reviews/mark-verified` is the only verified-entry path.

Existing product gap:

- The app could display extraction results and manually save/verify fields, but there was no explicit controlled endpoint to materialize extraction-derived review candidates as `pending` rows without entering verified state.
- Human Workbench/detail UI can display extraction results and call save/mark-verified flows, but persistent review queue preparation was missing at the backend seam.

D4-3A code change:

- Added `ExtractionReviewService.prepare_pending_reviews(paper_id)`.
- Added `POST /api/extraction/results/{paper_id}/reviews/prepare`.
- The new seam creates or refreshes only non-empty extraction-derived field review rows as:
  - `reviewer_status=pending`
  - `verified=False`
  - `reviewer=None`
  - `reviewer_note=prepared_from_extraction`
- It preserves verified rows if they already exist and does not call `mark_verified`.

Safety answers:

1. Real extraction output can now enter a human review queue as pending/unverified rows.
2. Entry point: `POST /api/extraction/results/{paper_id}/reviews/prepare`.
3. Missing piece before this gate: backend service/API seam; frontend UX was not changed.
4. `save_reviews` still cannot set `reviewer_status=verified`.
5. `mark_verified` remains the only path that writes verified reviews.
6. `unsafe_locator` still blocks export/writing; pending review rows do not unlock anything.

## 5. Tests Added

Added:

- `backend/tests/test_d4_single_paper_review_pilot.py`

Coverage:

- Extraction-derived candidate can be prepared as pending/unverified.
- Review preparation API returns pending-only rows.
- Prepared candidate cannot be turned verified through `save_reviews`.
- `save_reviews` cannot create safe verified review.
- `mark_verified` remains the only verified path.
- Missing exact locator blocks export and writing even with review-like payload.
- Exact locator plus human verified review is required before export and locator-bearing writing eligibility.
- Pilot preparation does not mutate `Paper` metadata.

## 6. Verification

Targeted verification:

- `py -m pytest tests\test_d4_single_paper_review_pilot.py tests\test_extraction_reviews_api.py tests\test_export_safety_gate.py tests\test_writing_safety_gate.py`
- Result: 27 passed, 134 warnings.

Full required verification:

- `py -m compileall app findpapers tests`
- Result: passed.
- `py -m pytest`
- Result: 327 passed, 566 warnings.

Frontend Playwright: not run because no frontend files were modified.

## 7. Safety Conclusion

The pilot path is ready as a backend-controlled preparation seam. It does not run extraction/reprocessing, does not materialize new scientific facts, does not mark verified, and does not make any active DB paper eligible for DFT export or writing.

The selected active paper remains blocked for export/writing until a future gate adds exact locator coverage and an explicit human `mark_verified` action.

## 8. Next Recommended Step

D4-3B should exercise the new preparation endpoint against one selected paper only under an explicit rollback or approved active-DB write plan, then inspect the pending review queue in the Human Workbench. Do not proceed to verified/export/writing until exact locators are present and a human reviewer explicitly uses `mark_verified`.

## 9. Prohibited Actions Check

- Active DB write: no
- DB copy/move/delete: no
- Registry write: no
- Migration apply: no
- Verified review write: no
- Extraction/reprocessing apply: no
- Full materialize: no
- Artifact cleanup: no
- Text-only/missing-page/unsafe locator used as safe evidence: no
