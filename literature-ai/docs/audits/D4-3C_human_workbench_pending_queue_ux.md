# D4-3C Human Workbench Pending Queue UX Verification

Date: 2026-05-27

Scope: frontend/Human Workbench UX verification for the D4 pilot paper pending review queue. No real active DB write, verified review write, export, writing export, extraction/reprocessing apply, materialize, migration apply, or artifact cleanup was performed.

## 1. Baseline / Sync

Required preflight commands:

- `git status --short`: clean
- `git log -1 --oneline`: `badfdd8 docs d4 single paper active pending review smoke`
- `git rev-parse HEAD`: `badfdd8af739343bf4a3ba78128f9a8daf102403`
- `git branch -vv`: `* master badfdd8 [origin/master] docs d4 single paper active pending review smoke`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `badfdd8af739343bf4a3ba78128f9a8daf102403 refs/heads/master`

Conclusion:

- local `HEAD`, local `origin/master`, and remote `refs/heads/master` were identical at `badfdd8af739343bf4a3ba78128f9a8daf102403`.
- Starting worktree was clean.

## 2. Active DB / Pilot Paper Read-only Confirmation

Pilot paper:

- `paper_id`: `3978dc79f94f4457863fd68449ae293d`
- title: `锂硫电池非均相电催化剂`

Read-only runtime notes:

- `http://localhost:8000` active backend was not running in this local session, so direct active API reads were unavailable.
- `app.utils.active_database.get_active_database_info()` in this checkout resolves to a local 5-paper backup SQLite, not the D4-3B 15-paper active DB described in the previous manifest.
- Repository-local SQLite files in this checkout did not contain the D4 pilot paper.
- Therefore D4-3C frontend/API visibility was verified with a controlled Playwright mock, which is allowed for this gate and does not write the real active DB.

## 3. API Visibility Result

Controlled mock API responses used by the D4-3C Chromium smoke:

- `GET /api/extraction/results/3978dc79f94f4457863fd68449ae293d/reviews`: 5 rows
- all 5 rows: `reviewer_status=pending`
- all 5 rows: `verified=false`
- row ids:
  - `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896`
  - `09f83676-8f13-4e82-a576-ab359b264933`
  - `280f2d9e-3ebb-4107-9702-f6ea6d645465`
  - `4ba0e490-5934-439c-8136-33a8ddf4e201`
  - `56f72584-45b3-465b-9a40-97ec60a2fabf`
- `GET /api/extraction/results/3978dc79f94f4457863fd68449ae293d`: `field_reviews=5`
- locator state exposed to frontend: `missing_page / unsafe_locator / no exact locator`
- evidence text present for each mocked pending field

No `POST /reviews/prepare`, `POST /reviews/mark-verified`, export, or writer request was made by merely opening the workbench page.

## 4. Human Workbench Visibility Result

Observed through Chromium Playwright on `external_analysis_workbench/index.html?paper_id=3978dc79f94f4457863fd68449ae293d`:

- Human Workbench loaded the pilot paper metadata.
- The audit summary surfaced 5 review rows.
- Pending rows were visible across:
  - `CatalystSample`: `name`, `catalyst_type`, `metal_centers`
  - `DFTSetting`: `convergence_settings`
  - `ElectrochemicalPerformance`: `rate`
- Each visible pending field showed evidence text.
- Each visible pending field showed `missing_page` and explicit missing exact locator risk wording.
- No PDF jump/highlight button was available for these missing locator rows.

## 5. UX Wording Audit

UI code changed:

- pending status chip now displays `Pending human review / Not verified`
- missing locator hint now displays `Exact PDF locator missing / missing_page / unsafe_locator / no exact locator`
- missing locator hint also states `Blocked from export/writing until exact locator + human verification`

Blocked/misleading wording audit:

- No pending field card displayed `Human verified`.
- No pending field card displayed `Ready for export`.
- No pending field card displayed `Ready for writing`.
- No pending field card displayed export-ready or writing-ready wording.
- DFT export/writing eligibility remained blocked by wording and by absence of any export/writer request on page open.

## 6. Tests Added

Updated `frontend/tests/smoke.spec.js`:

- added D4 pilot paper mock
- added 5 pending review mock rows with the requested row ids
- added D4-3C smoke test covering:
  - pending rows visible in Human Workbench
  - pending/unverified wording visible
  - unsafe/missing exact locator wording visible
  - no PDF jump/highlight controls for missing-page rows
  - no verified/safe/export-ready/writing-ready misleading wording in the pending field area
  - no prepare call on page open
  - no mark-verified call on page open
  - no frontend request body sends `reviewer_status=verified` or `verified=true`
  - no export/writer request on page open

## 7. Screenshots / Page Observations

No screenshot artifact was retained. Page observations were captured by Playwright assertions:

- `#paperMeta` contained the pilot `paper_id`.
- `#stabilitySummaryBox` contained total review count `5`.
- `#schemaForm` contained the pending review fields, evidence text, `missing_page`, `unsafe_locator`, and blocked export/writing wording.
- `#schemaForm button[onclick^="triggerWorkbenchLocatorAction"]` count was `0`.

## 8. Prepare Button Status

No prepare button was added in this gate.

If a real prepare entry point is needed, it should be handled as a separate D4-3D product gate because it would introduce real active-write UX and needs its own safety controls.

## 9. Verification

`npm` was not available in PATH.

D3-5.1 documented fallback used:

1. Start independent static server:
   `py -m http.server 8000`
2. Use temporary `%TEMP%\d3-playwright-shim\@playwright\test` shim.
3. Run bundled Playwright:
   `C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe C:\Users\zhaob\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright\cli.js test --project=chromium`

Results:

- D4-3C focused run: `1 passed`
- full frontend Chromium smoke: `74 passed (1.8m)`

Backend tests were not run because no backend API/shared contract code changed.

## 10. Safety Conclusion

- active DB write: no
- verified review write: no
- `mark_verified`: not called
- `reviewer_status=verified`: not sent by D4-3C open-page flow
- `verified=true`: not sent by D4-3C open-page flow
- `save_reviews` writing verified: not touched
- export: not run
- writing export/final report export: not run
- extraction/reprocessing apply: not touched
- materialize: not touched
- migration apply: not touched
- artifact cleanup: not touched

D4-3C passes the frontend UX lock with controlled mock data: pending rows are visible, clearly unverified, explicitly blocked by missing exact locator risk, and do not imply export/writing readiness.

## 11. Next Recommended Gate

D4-3D should decide whether to add a controlled prepare entry point. That gate should explicitly cover active-write UX, permissions, idempotency, and no accidental prepare calls on page load.
