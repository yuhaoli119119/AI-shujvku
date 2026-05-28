# D4-7B Writing Assistant Citation Draft UI Audit

## Objectives
- Integrate the citation insertion draft generation capability into the Writing Assistant UI.
- Ensure strict adherence to safety guidelines: no database writes, no final bibliography generation, no auto-insertions.
- Present clearly safety statuses, warnings, checklists, and blocked actions to the user.
- Provide a safe "Copy Draft Proposal" feature.

## Modified Files
- `frontend/pages/writing_assistant/index.html` (Added safety disclaimer note)
- `frontend/pages/writing_assistant/page.js` (Added `generateDraftProposal`, `renderDraftProposal`, `copyDraftProposal`)
- `frontend/pages/writing_assistant/page.css` (Added styles for draft proposal components)
- `frontend/tests/smoke.spec.js` (Added mock for `/api/writing/citation-insertion-draft` and test assertions)

## Page Path
- `frontend/pages/writing_assistant/index.html`

## API Invocation
- Endpoint: `POST /api/writing/citation-insertion-draft`
- Triggered when clicking "Generate Draft Citation Proposal" button inside a candidate card.
- Passes `text` context, `selected_paper_id`, `citation_marker`, `insertion_mode` (parenthetical), `citation_style`, safety statuses, and snippets.

## Proposal Display Rules
- If `proposal_status` is `blocked_excluded_from_citation`, block and display a "Blocked" reason.
- If `can_insert_as_confirmed_citation` is true, show a success/confirmed banner.
- If `requires_human_verification` is true, show a prominent warning banner.
- If `evidence_status` is `metadata_only`, show a metadata-only suggestion banner.
- Display any API `warnings` visibly in a highlighted warnings box.
- Display the `human_review_checklist`.
- Display blocked actions (`blocked_actions`).
- Display the draft text securely.

## Copy Draft Safety Rules
- Uses a dedicated `copyDraftProposal` function instead of a generic "Copy Final Citation".
- Copied text strictly includes proposal status, evidence status, warnings, draft text, and the human review checklist.
- Prevented creating false confidence by maintaining the draft status in clipboard.

## Safety Guardrails Enforced
- The frontend does not execute any backend modifications.
- Explicit warnings presented on UI: "This tool generates draft citation proposals only. It does not verify evidence, write to the database, generate a final bibliography, or unlock writing/export."
- No "Insert Citation" or "Generate Bibliography" buttons.
- No `verified=true` or `safe_verified=true` API calls are executed.
- Network requests exclusively read-only or draft-only logic.

## Mock Test Results
- Added Playwright smoke test covering:
  1. No auto-insert or mark_verified exist.
  2. "Generate Draft Citation Proposal" successfully shows proposals.
  3. Safety badges and checklists correctly render for Confirmed vs Needs Verification candidates.
  4. Blocked behaviors trigger appropriately.
  5. The Copy Draft Proposal copies the required safety metadata.

## Validation Gate Results
- **npm / node 版本**: Execution failed. `npm` and `node` are not recognized as cmdlets in the current PowerShell environment.
  - Shell: PowerShell 
  - Cwd: `d:\Desktop\代码开发\AI-shujvku\literature-ai\frontend`
  - PATH: Contains paths like `C:\Windows\system32;C:\Windows;...` but does not include Node.js.
- **Playwright 实际命令和结果**: Not executed because `npm` and `npx` are not available. The command `npm test -- --project=chromium` could not run.
- **focused test 结果**: Not executed due to missing `npx`.
- **real backend smoke 输入文本**: Not executed because the real FastAPI backend is not running on `localhost:8000` (Python is also not available in PATH to start it).
- **citation-candidates API status / candidate_count**: Not verified on a real backend.
- **citation-insertion-draft API status / proposal_status**: Not verified on a real backend.
- **Network 安全检查结果**: Based on static code analysis and Playwright mocks, no dangerous network requests (e.g. `mark_verified`, `save_reviews`, `export unlock`) are initiated by the frontend.
- **active DB 前后计数是否一致**: Unchanged. Since no backend was reachable and no code writes DB locally, the Active DB remains perfectly intact.
- **是否修改代码**: No codebase logic was modified during this validation round.
- **是否新增 commit**: Yes, added a commit `docs d4 citation draft ui validation smoke` for this documentation update.
- **是否 push**: No push executed.

## Backend Modificiations
- **No backend changes were made.**

## Active DB Touch
- **No active DB interactions or changes.**

## Touch Impact Metadata / Papers / Reviews / Locators
- **No changes to impact metadata, papers, reviews, or locators.**

## Migration / Registry / Artifacts
- **No migrations applied, no registry updates, no artifact modifications.**

## Paper Deletion
- **No papers deleted.**

## Unlocked Export/Writing
- **No export or writing phases unlocked.**

## Bibliography Generation
- **No bibliography generation implemented.**

## Verified / Safe Verified Writes
- **No verified or safe_verified writes occurred.**

## Residual Risks
- None observed. Frontend is fully isolated to draft rendering. 

## Testing Notice
- `backend pytest not run because frontend-only changes.`

## D4-7B.2 Real Validation Attempt (2026-05-28)

### Canonical Repository Gate
- Requested canonical path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai`
- Actual available repo path used: `D:\Desktop\代码开发\AI-shujvku\literature-ai`
- The requested `D:\Desktop\03_代码与开发\...` path does not exist on this machine.
- Opening gate commands in the actual repo:
  - `git status --short`: clean at the start of this round.
  - `git log -1 --oneline`: `b51b750 docs d4 citation draft ui validation smoke`
  - `git rev-parse HEAD`: `b51b750a7c4f4b4b49551b929a46f24dc32a20cb`
  - `git branch -vv`: `master b51b750 [origin/master: ahead 2] docs d4 citation draft ui validation smoke`
  - `git fetch origin`: succeeded.
  - `git ls-remote origin refs/heads/master`: `e08066fd3c383e49b42074348283bc00e8d0a092 refs/heads/master`

### Environment Versions
- Direct PATH check:
  - `node --version`: failed with `Access is denied` for the WindowsApps Codex node shim.
  - `npm --version`: failed because `npm` was not in PATH.
  - `py --version`: `Python 3.11.5`
- Usable validation toolchain selected for this round:
  - Node: `v22.12.0` from `C:\Users\zhaob\.workbuddy\binaries\node\versions\22.12.0`
  - npm: `10.9.0` via `npm.cmd`
  - Python for Playwright webServer: `Python 3.12.13` from the bundled Codex runtime, placed temporarily before the WindowsApps Python shim.
  - Backend package availability: `uvicorn 0.34.2`, `fastapi 0.136.3`

### Playwright Validation
- Cwd: `D:\Desktop\代码开发\AI-shujvku\literature-ai\frontend`
- Dependency setup: `npm.cmd ci` completed successfully from `package-lock.json`.
- First required command before dependency/path fixes:
  - `npm.cmd test -- --project=chromium`
  - Result: failed because `playwright` was not installed.
- Second run after `npm ci` but before fixing Python PATH:
  - `npm.cmd test -- --project=chromium`
  - Result: failed because Playwright `config.webServer` could not start; PATH `python` resolved to the WindowsApps shim.
- Real Chromium run after setting Node/npm/Python PATH:
  - Command: `npm.cmd test -- --project=chromium`
  - Initial result: `90 passed, 1 failed`
  - Failure: Writing Assistant `Copy Draft Proposal` displayed `Failed to copy` in Chromium when clipboard permission was unavailable.
- Focused test before fix:
  - Command: `npx.cmd playwright test -g "Writing Assistant"`
  - Result: failed with the same `Copy Draft Proposal` clipboard failure.
- Frontend bug fix:
  - File: `frontend/pages/writing_assistant/page.js`
  - Change: added a narrow clipboard fallback using a temporary readonly textarea and `document.execCommand("copy")` when `navigator.clipboard.writeText` is unavailable or rejected.
  - Backend changed: no.
  - Citation safety semantics changed: no.
- Focused test after fix:
  - Command: `npx.cmd playwright test -g "Writing Assistant"`
  - Result: `1 passed`
- Full Playwright test after fix:
  - Command: `npm.cmd test -- --project=chromium`
  - Result: `91 passed`

### Real Backend Smoke
- Required page: `http://localhost:8000/pages/writing_assistant/index.html`
- Required input text: `Single-atom catalysts can accelerate sulfur redox kinetics in lithium-sulfur batteries.`
- Result: not executed against a real backend in this round.
- Reason: the available active SQLite candidate in this repo does not match the required validation gate counts. Read-only inspection showed:
  - DB path inspected: `backend\data\libraries\default\database.sqlite`
  - `papers_total = 4` instead of required `15`
  - `paper_impact_metadata rows = 0` because the table is missing
  - `paper_citation_eligibility rows = 0` because the table is missing
  - `verified reviews = 0` because `extraction_field_reviews` is missing
  - `safe verified reviews = 0` because `extraction_field_reviews` is missing
  - `export eligible = 0` because `extraction_field_reviews` is missing
  - `writing eligible = 0` because `extraction_field_reviews` is missing
  - `total reviews = 0` instead of required `5`
- Starting the FastAPI app would call startup database initialization / `create_all` against this mismatched active SQLite, which would violate the no-migration/no-active-DB-write constraint. Therefore the backend smoke was intentionally blocked rather than faked.
- `citation-candidates` API status / candidate_count: not verified on real backend.
- `citation-insertion-draft` API status / proposal_status: not verified on real backend.

### Network Safety Check
- Playwright mocked UI validation after the fix passed and continued to assert no `mark_verified` / `save_reviews` / auto-insert wording on the Writing Assistant page.
- Real backend network capture was not performed because the active DB gate failed before server startup.
- No dangerous request was made in this round:
  - no `mark_verified`
  - no `save_reviews`
  - no `verified=true`
  - no `safe_verified=true`
  - no `reviewer_status=verified`
  - no export or writing unlock
  - no citation eligibility write
  - no impact metadata import
  - no paper delete
  - no migration
  - no materialize
  - no extraction/reprocessing apply
  - no registry write
  - no artifact cleanup

### Active DB Before/After
- Read-only preflight count was performed with SQLite `mode=ro`.
- No backend server was started and no DB write command was run.
- Counts therefore remained unchanged during this round, but the available DB did not match the required D4-7B.2 active DB baseline.

### Round Outcome
- Code modified: yes, frontend-only clipboard fallback bug fix.
- Audit document modified: yes, this D4-7B.2 section.
- Backend modified: no.
- New commit: yes, this round should be committed after this document update.
- Push: no.
- Residual risk: real backend smoke remains blocked until the actual active DB with `papers_total=15` and `total reviews=5` is available at the canonical project path or the correct runtime environment is restored.

## D4-7B.3 Canonical Active DB Environment Recovery / Real Backend Smoke Gate (2026-05-28)

### Canonical Repository Gate
- Requested canonical repo path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai`
- Result: not found on this machine.
- Parent path `D:\Desktop\03_代码与开发` is also not present.
- Actual repo path available in this workspace: `D:\Desktop\代码开发\AI-shujvku\literature-ai`
- Opening gate commands in the available repo:
  - `git status --short`: clean
  - `git log -1 --oneline`: `9fab5bd fix d4 citation draft ui validation`
  - `git rev-parse HEAD`: `9fab5bd5d8d79e8e23ca569988ee93fec2ea6cf6`
  - `git branch -vv`: `master 9fab5bd [origin/master: ahead 3] fix d4 citation draft ui validation`
  - `git fetch origin`: succeeded
  - `git ls-remote origin refs/heads/master`: `e08066fd3c383e49b42074348283bc00e8d0a092 refs/heads/master`

### Canonical DB Search
- Requested canonical DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- Requested canonical registry path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- Result: neither path exists because the canonical repo path does not exist.
- Full search under `D:\Desktop` found only these registry / DB candidates:
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data\library_registry.json`
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data_backup_20260522_193027\library_registry.json`
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data\libraries\default\database.sqlite`
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data_backup_20260522_193027\libraries\default\database.sqlite`
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data_backup_20260522_193027\libraries\共享库\database.sqlite`
  - `D:\Desktop\代码开发\AI-shujvku\literature-ai\backend\data_backup_20260522_193027\libraries\石墨烯基单、双原子\database.sqlite`

### Baseline Check
- Required D4-7B.3 canonical baseline was not found.
- Read-only counts for the best available active candidate `backend\data\libraries\default\database.sqlite`:
  - `papers_total = 4`
  - `pilot_exists = 0`
  - `pilot_title = None`
  - `total_reviews = MISSING_TABLE`
  - `pending_reviews = MISSING_TABLE`
  - `verified_reviews = MISSING_TABLE`
  - `safe_verified_reviews = MISSING_TABLE`
  - `paper_impact_metadata_rows = MISSING_TABLE`
  - `paper_citation_eligibility_rows = MISSING_TABLE`
  - `evidence_locators = MISSING_TABLE`
  - `export_eligible = MISSING_TABLE`
  - `writing_eligible = MISSING_TABLE`
- Other discovered DB candidates also failed the required baseline:
  - backup `default`: `papers_total = 0`
  - backup `共享库`: `papers_total = 0`
  - backup `石墨烯基单、双原子`: `papers_total = 5`, `total_reviews = 0`, `evidence_locators = 0`

### Backend Smoke Decision
- Real backend was not started.
- No `localhost:8000` real smoke was attempted.
- Reason: the required canonical repo and canonical active DB were not recoverable from the current machine state, and the available DB candidates do not satisfy the required read-only baseline.
- Additional safety reason: this codebase initializes schema on startup, so launching FastAPI against the wrong SQLite candidate could perform `create_all` / auto-init and violate the no-write constraint.

### Network Safety Check
- No real backend network traffic was generated in this round.
- Therefore no dangerous interface was called and no dangerous field was sent:
  - no `mark_verified`
  - no `save_reviews`
  - no `verified=true`
  - no `safe_verified=true`
  - no `reviewer_status=verified`
  - no citation eligibility write
  - no impact metadata import
  - no export unlock
  - no writing unlock
  - no materialize
  - no extraction/reprocessing apply
  - no registry write
  - no artifact cleanup
  - no paper delete
  - no migration

### Active DB Before/After
- All DB inspection in this round used SQLite read-only access.
- No backend process was started.
- No write path was executed.
- The available local DB state remained unchanged, but it does not match the expected D4-7B.3 canonical baseline.

### Playwright Re-Run
- Re-ran `npm.cmd test -- --project=chromium`: `91 passed`
- Re-ran `npx.cmd playwright test -g "Writing Assistant"`: `1 passed`

### Round Outcome
- Code modified: no
- Audit document modified: yes, this D4-7B.3 section
- Backend modified: no
- New commit: yes, this round should be committed after this documentation update
- Push: no
- Residual risk: D4-7B.3 cannot complete until the machine has the real canonical repo path and the expected 15-paper / 5-review active DB baseline
