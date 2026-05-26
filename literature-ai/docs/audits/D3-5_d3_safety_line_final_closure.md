# D3-5 D3 Safety Line Final Closure / Integrated Regression Gate

Date: 2026-05-27

Scope: final closure verification only. This pass did not add features, change business logic, or modify the D3 safety contract.

## 1. Starting State

- Starting HEAD full hash: `b3b02156b40d43f98b5344c09cc0f8e1a30c338c`
- `origin/master`: `b3b02156b40d43f98b5344c09cc0f8e1a30c338c`
- Remote `refs/heads/master`: `b3b02156b40d43f98b5344c09cc0f8e1a30c338c`
- Worktree state before closure manifest: clean (`git status --short` returned no entries)
- Branch: `master` tracking `origin/master`
- Baseline sync conclusion: local HEAD, local `origin/master`, and remote `refs/heads/master` were identical before any edit.

## 2. D3 Closed Items Reviewed

The following D3 closure chain was treated as already closed and was revalidated through audit manifest review plus integrated regression:

- D3-2A backend safety contract
- D3-2B frontend safety adaptation
- D3-2C controlled UX walkthrough
- D3-2C.1 walkthrough audit manifest
- D3-2C.2 requests dependency reproducibility
- D3-2C.3 async pytest reproducibility
- D3-3 backend review workflow regression lock
- D3-3.1 regression lock audit manifest
- D3-4 frontend contract lock / UX regression
- D3-4.1 export empty / blocked-only lock
- D3-4.1.1 export empty / blocked-only verification
- D3-4.2 frontend toolchain repro note

Reviewed audit manifests:

- `literature-ai/docs/audits/D3-2C_controlled_ux_walkthrough.md`
- `literature-ai/docs/audits/D3-3_review_workflow_regression_lock.md`
- `literature-ai/docs/audits/D3-4_frontend_contract_lock.md`
- `literature-ai/docs/audits/D3-4.1_export_empty_blocked_only_lock.md`
- `literature-ai/docs/audits/D3-4.2_frontend_toolchain_repro.md`

All five required manifests exist. The reviewed contents align with their D3 phase purpose. Some older Chinese prose renders with encoding mojibake in the existing files, but no safety-contract contradiction or closure-blocking manifest gap was found in this pass.

## 3. Backend Validation

- Python: `Python 3.11.5`
- Python detail: `3.11.5 (tags/v3.11.5:cce6ba9, Aug 24 2023, 14:38:34) [MSC v.1936 64 bit (AMD64)]`
- Compile command: `py -m compileall app findpapers tests`
- Compile result: passed; all target package/test directories listed without compile errors.
- Pytest command: `py -m pytest`
- Pytest result: `309 passed, 5 warnings in 272.47s (0:04:32)`
- Initial pytest attempt note: the first identical `py -m pytest` invocation was interrupted by the local tool timeout at about 304 seconds and was rerun with a longer timeout; the rerun passed.
- Warnings summary:
  - `DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute`
  - `DeprecationWarning: builtin type SwigPyObject has no __module__ attribute`
  - `DeprecationWarning: builtin type swigvarlink has no __module__ attribute`
- Backend failures: none.

## 4. Frontend Validation

- Standard npm command attempted: yes.
- Standard command: `npm test -- --project=chromium`
- Standard command result: failed because `npm` is not in `PATH` in this shell (`CommandNotFoundException`).
- Fallback used: yes, following the D3-4.2 bundled Node / Playwright path.
- Static server command: `py -m http.server 8000` from `literature-ai/frontend`.
- Node executable: `C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe`
- Playwright CLI: `C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\cli.js`
- Additional module resolution path required in this runtime:
  - `C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\.pnpm\node_modules`
  - Temporary non-repo shim for `@playwright/test` to `playwright/test`: `C:\Users\zhaob\AppData\Local\Temp\d3-playwright-shim`
- Actual fallback command:
  - `$env:NODE_PATH='C:\Users\zhaob\AppData\Local\Temp\d3-playwright-shim;C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\.pnpm\node_modules;C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'; C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\cli.js test --project=chromium`
- Chromium Playwright result: `73 passed (1.8m)`
- Frontend failures: none.

## 5. D3 Safety Invariants Confirmed

Integrated regression plus prior D3 manifests confirm the D3 safety line remains closed:

- AI candidate cannot become verified directly.
- `save_reviews` cannot set `verified`.
- `mark_verified` is the only verified entry.
- `mark_verified` requires evidence reference plus `evidence_text`.
- Export and writing only use safe verified rows with required evidence.
- `candidate_ids=[]` cannot mean full materialize.
- Full materialize requires `explicit_all=true` plus frontend confirmation.
- Empty export does not show misleading success.
- Blocked-only export does not imply unsafe rows were exported.
- Dangerous bypass wording remains blocked in relevant UI areas.

## 6. Explicit Non-Actions

This closure pass did not touch:

- Active DB
- Migration apply
- Verified review write
- Extraction apply
- Real full materialize
- Canonical registry
- Historical mirror cleanup
- Real artifacts cleanup
- Frontend business UX redesign
- Backend safety contract behavior

## 7. Remaining Risk

- `npm` remains unavailable in this shell's `PATH`; frontend verification still requires the documented bundled Node / Playwright fallback unless the environment PATH is fixed.
- The current bundled runtime exposes Playwright through a pnpm-style layout, so `NODE_PATH` must include `.pnpm\node_modules`.
- This runtime does not expose `@playwright/test` directly; a temporary non-repo shim was required to map `@playwright/test` to `playwright/test`.
- Backend third-party SWIG-related deprecation warnings remain present but did not fail the suite.
- Existing historical audit manifests include some Chinese text rendered with encoding mojibake; this pass did not rewrite prior audit documents because their closure facts and safety-contract meaning were still reviewable.
