# D3-3 Review Workflow Regression Lock Audit

## Start Commit

`f3a5d4074afcf86d723e73ca09af0ca6b401d563`

## D3-3 Change Summary

D3-3 added one backend regression lock test file:

- `literature-ai/backend/tests/test_d3_review_workflow_regression_lock.py`

## Coverage Matrix

### A. External AI Candidate / Materialize

- `candidate_ids=[]` must return 400.
- Omitted `candidate_ids` without `explicit_all=true` must return 400.
- Full materialize requires `explicit_all=true`.
- Single-item / multi-select materialize must send explicit `candidate_ids`.
- Materialize can only create pending note/correction/relationship artifacts.
- Materialize cannot create `reviewer_status=verified`.
- Verified-like payload cannot create a verified review.
- Internal AI `auto_apply` cannot directly write Paper fields and cannot auto-verify.

### B. Review Save / Verify

- `save_reviews` cannot set `reviewer_status=verified`.
- `save_reviews` cannot overwrite an existing verified review.
- `mark_verified` is the only verified entry point.
- `mark_verified` must require an evidence reference.
- `mark_verified` must require `evidence_text`.
- Stale / ambiguous / unresolved / unknown / missing reviews cannot become safe verified.

### C. Export

- DFT export defaults to only safe verified rows with required evidence.
- Unsafe rows are blocked.
- Rows with missing evidence are blocked.
- Response headers include:
  - `X-D3-Export-Safety-Gate`
  - `X-D3-Export-Count`
  - `X-D3-Block-Count`
- Mixed safe/unsafe/missing-evidence exported count and block count are stable.

### D. Writing / RAG

- Writing can only use safe verified `evidence_chain` payloads.
- Missing `evidence_chain` is blocked.
- Unsafe review payloads are blocked.
- Stale / ambiguous / unresolved / unknown review payloads are blocked.
- Serialized verified-looking unsafe review payloads cannot bypass the safe gate.

### E. Frontend Contract Awareness

Frontend smoke already covers:

- It does not send `candidate_ids: []`.
- Full materialize payload uses `explicit_all: true`.
- Full materialize requires a second confirmation.
- Old dangerous wording regression checks.
- Save / Verify scope is visible.
- DFT export safe gate is visible.

D3-3 did not change frontend code, so Playwright was not run.

## Verification Results

- `py -m compileall app findpapers tests`: passed.
- `py -m pytest`: 309 passed, 5 warnings.
- Frontend Playwright: not run because frontend was unchanged.

## Active DB / Safety Statement

D3-3 regression tests used temporary SQLite databases and did not touch the active DB.

Not touched:

- real data
- migration
- verified review
- extraction apply
- active DB move
- real full materialize
- UI
- business logic
- D3 safety contract

## Remaining Risks

- Local `python` points to the WindowsApps stub; actual verification used `py -m`.
- Third-party Swig deprecation warnings remain.
- Frontend contract depends on existing smoke coverage; D3-3 did not add a frontend-specific regression test.
