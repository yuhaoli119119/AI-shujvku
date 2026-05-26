# D4-1 Real Extraction Workflow Baseline & Safety Lock Gate

Date: 2026-05-27

Scope: establish the first D4 real-extraction baseline and safety locks. This pass reviewed extraction, reprocessing, evidence locator degradation, review verification, writing/export gates, active DB helpers, and artifact path handling. It added focused backend regression tests and this audit manifest only.

## 1. Starting State

Commands requested and executed before any edit:

- `git status --short`: clean
- `git log -1 --oneline`: `46b33ea docs d4 next phase readiness triage`
- `git rev-parse HEAD`: `46b33ea8ab95b4af795b1bf20abebbca8ce6cca7`
- `git branch -vv`: `master 46b33ea [origin/master] docs d4 next phase readiness triage`
- `git fetch origin`: first two attempts hit transient GitHub TLS `SSL_ERROR_SYSCALL`; third attempt succeeded.
- `git ls-remote origin refs/heads/master`: `46b33ea8ab95b4af795b1bf20abebbca8ce6cca7 refs/heads/master`

Baseline conclusion after successful fetch:

- Starting HEAD: `46b33ea8ab95b4af795b1bf20abebbca8ce6cca7`
- Local `origin/master`: `46b33ea8ab95b4af795b1bf20abebbca8ce6cca7`
- Remote `refs/heads/master`: `46b33ea8ab95b4af795b1bf20abebbca8ce6cca7`
- Worktree before edits: clean.
- HEAD, local `origin/master`, and remote `refs/heads/master` were identical before any file change.

## 2. Reviewed Scope

### 2.1 Extraction / Reprocessing

Reviewed:

- `backend/app/services/paper_reprocessing.py`
- `backend/app/services/extraction_pipeline.py`
- `backend/app/api/extraction.py`
- `backend/app/api/papers/detail.py`
- `backend/app/services/workflow_jobs.py`
- Existing extraction/reprocessing tests, especially `test_paper_reprocessing.py`, `test_extraction_pipeline.py`, and `test_extraction_reviews_api.py`

Findings:

- The real reprocessing apply path is `POST /api/papers/{paper_id}/extract`, which calls `PaperReprocessingService.rerun_stage2()` and commits Stage 2 replacement. This path is intentionally not run against the active DB in D4-1.
- Stage 2 persistence writes extracted entities plus evidence spans/locators, but it does not create `ExtractionFieldReview` rows or mark anything verified by itself.
- Reprocessing uses persisted paper artifacts and DB rows to rebuild a `UnifiedPaperDocument`; D4 product hardening can build on this, but any real-paper apply must be a separate approved task.

### 2.2 Evidence Chain / Page-BBox Downgrade

Reviewed:

- `backend/app/services/evidence_locator_service.py`
- `backend/app/utils/locator_degradation.py`
- `backend/app/services/evidence_service.py`
- `backend/tests/test_evidence_page_recovery.py`
- `backend/tests/test_evidence_api.py`
- `backend/tests/test_evidence_quality_audit.py`

Findings:

- Missing page is represented as `text_only`, `missing_page`, or `missing_locator` depending on provenance and evidence text; it is not converted into a fake page.
- Missing bbox never unlocks PDF highlight. Current degradation deliberately keeps `can_highlight_in_pdf=False`, including exact-page locators without bbox.
- Approximate or ambiguous page matches are not serialized as exact jump/highlight targets.

### 2.3 Review / Verified Workflow

Reviewed:

- `backend/app/services/extraction_review_service.py`
- `backend/app/services/review_target_resolver.py`
- `backend/app/utils/review_safety.py`
- `backend/tests/test_d3_review_workflow_regression_lock.py`
- `backend/tests/test_review_boundary_enforcement.py`

Findings:

- `save_reviews` cannot set `reviewer_status=verified`.
- `mark_verified` remains the verified entry point and checks target existence, evidence reference, and evidence text.
- Safe verified serialization depends on both `reviewer_status=verified` and safe target resolution status (`active` or `remapped`).
- AI/external/extraction output cannot become verified by merely existing.

### 2.4 Writing / Export Safe Gate

Reviewed:

- `backend/app/api/papers/aggregation.py`
- `backend/app/rag/retriever.py`
- `backend/app/rag/writer.py`
- `backend/app/utils/review_safety.py`
- `backend/tests/test_export_safety_gate.py`
- `backend/tests/test_writing_safety_gate.py`

Findings:

- DFT export remains gated by safe verified review plus required evidence reference and evidence text.
- Retriever keeps unsafe extracted DFT/electrochemical/mechanism rows out of writing retrieval.
- Writing cards require a safe reviewed evidence chain payload; text evidence by itself is not enough.

### 2.5 Active DB Helpers

Reviewed:

- `backend/app/utils/active_database.py`
- `backend/app/api/health.py`
- D2/D3 audit manifests describing the closed active DB/migration line
- Existing active DB tests, especially `test_active_database_recovery.py`, `test_d2_*`, and `test_shadow_registry_hygiene_gate.py`

Findings:

- `get_active_database_info()` is a read/status helper.
- `activate_active_library_database()` and real runtime DB switching were not used in this pass.
- D4-1 tests monkeypatch registry, workspace roots, backend roots, and settings into temp paths before calling active DB status logic.

### 2.6 Artifact Paths

Reviewed:

- `backend/app/utils/artifact_paths.py`
- Paper detail PDF serving and deletion safeguards in `backend/app/api/papers/detail.py`
- Existing artifact path recovery tests in `test_active_database_recovery.py`

Findings:

- Artifact path resolution searches configured storage roots and known storage categories; D4-1 did not move, delete, rebuild, or clean real artifacts.
- The new D4 tests use temp SQLite and temp storage settings only.

## 3. Added / Confirmed Safety Locks

New test file:

- `backend/tests/test_d4_real_extraction_baseline_lock.py`

Added tests:

1. `test_extraction_evidence_without_page_or_bbox_remains_text_only`
   - Builds extraction-like evidence in temp SQLite.
   - Confirms missing page and bbox remain absent.
   - Confirms serialized locator is `text_only`, `text_evidence_only`, no PDF jump, and no PDF highlight.

2. `test_extraction_output_does_not_create_verified_review_or_unlock_export`
   - Persists a DFT extraction result through the Stage 2 persistence path in temp SQLite.
   - Confirms no `ExtractionFieldReview` is created.
   - Confirms export gate blocks the row with `missing_review`.

3. `test_text_only_evidence_with_unsafe_review_cannot_enter_writing_or_export`
   - Builds text-only DFT evidence plus an unsafe stale verified-looking review.
   - Confirms export gate blocks with `unsafe_review`.
   - Confirms retriever does not include the unsafe DFT result in writing retrieval.

4. `test_text_only_writing_evidence_without_safe_review_payload_is_blocked`
   - Builds a writing card with text-only evidence but no safe review payload.
   - Confirms writing gate blocks with `missing_review`.

5. `test_active_database_info_uses_temp_registry_and_temp_sqlite_only`
   - Builds a temp registry and temp `database.sqlite`.
   - Monkeypatches active DB helper roots/settings to temp paths.
   - Confirms active DB status logic resolves only the temp SQLite path.

Confirmed invariants:

- Missing page does not fabricate page number.
- Missing page does not claim exact page locator.
- Missing bbox does not fabricate bbox or PDF highlight.
- Text-only evidence can remain evidence, but it cannot masquerade as a precise locator.
- Extraction output does not auto-create verified review.
- Extraction output does not bypass `mark_verified`.
- Extraction output does not enter export/writing safe paths without review/evidence safety gates.
- Tests use temp SQLite and temp directories only.
- Active DB was not written.

## 4. Test Results

Targeted D4 test:

- Command: `py -m pytest tests/test_d4_real_extraction_baseline_lock.py -q`
- Result: `5 passed in 6.97s`

Compile:

- Command: `py -m compileall app findpapers tests`
- Result: passed; all listed target packages and tests compiled without errors.

Full backend pytest:

- Command: `py -m pytest`
- Result: `314 passed, 5 warnings in 299.78s (0:04:59)`

Warnings summary:

- `DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute`
- `DeprecationWarning: builtin type SwigPyObject has no __module__ attribute`
- `DeprecationWarning: builtin type swigvarlink has no __module__ attribute`

Frontend Playwright:

- Not run. No frontend files or UI behavior were changed in D4-1.

## 5. Explicit Non-Actions

This D4-1 pass did not touch:

- Active DB
- Migration apply
- Active DB move
- Canonical registry
- Historical mirror cleanup
- Verified review write in any real database
- Extraction apply against real papers
- Real full materialize
- Real artifact cleanup
- Frontend UI
- Backend API behavior
- D3 safety contract behavior

## 6. Remaining Risks

- The 15 active papers still have not received a product-grade real extraction acceptance run in D4. This pass locks safety boundaries only.
- Page/bbox degradation is safe, but the product experience for text-only / missing-page evidence still needs a future UX/product pass.
- `npm` not being in PATH and the documented frontend Playwright fallback remain unrelated environment debt.
- SWIG deprecation warnings remain in backend pytest output.
- A future real extraction hardening task must explicitly decide whether it is read-only, snapshot-only, or allowed to perform real extraction apply against active papers.
