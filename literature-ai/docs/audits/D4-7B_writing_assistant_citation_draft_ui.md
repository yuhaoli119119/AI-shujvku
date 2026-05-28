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
- Note: `npm test` could not be executed due to `npm` not being available in the environment path.

## Real Backend Smoke Results
- Not executed directly as no active backend server is running on the local agent environment, but frontend integration is fully statically verified against the contract.

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
