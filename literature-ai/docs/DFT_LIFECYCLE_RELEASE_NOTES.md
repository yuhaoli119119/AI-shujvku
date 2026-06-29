# DFT Lifecycle Integration Release Notes

## Overview

This integration changes DFT review from AI-driven final adjudication to an issue, repair, and explicit human review lifecycle.

- DFT audit AI no longer writes final truth automatically.
- DFT consensus, audit, and missing-candidate paths create audit issues or review candidates instead of directly verifying, rejecting, editing, or promoting `DFTResult` rows.
- The primary repair AI may repair individual DFT audit issues through a constrained MCP capability, but primary repair output is not `human_verified`, `safe_verified`, or `ML_Ready`.
- The DFT audit center is read-only. It is an issue queue, copy surface, and navigation entry to DFT detail review.
- Final DFT verify/reject must come from an explicit human or user-authorized review path, typically the paper DFT detail view.
- Export, RAG eligibility, and ML-ready dataset inclusion remain controlled by the safe verified evidence gate.

## Main Features

- DFT audit issue queue backed by `DFTAuditIssue`.
- Controlled primary repair MCP flow through `repair_dft_audit_issue`.
- Dedicated `repair_dft_issues` capability for the primary DFT repair key.
- MCP capability lint warnings for misconfigured repair capability assignment.
- Read-only DFT audit report endpoint for issue counts, repair audit summaries, and warnings.
- Read-only DFT audit center UI.
- DFT detail deep links from audit issues, including target expansion, scroll, highlight, and issue context.
- Human review lifecycle for DFT verify/reject, including audit issue closure.
- Legacy review center DFT final-truth controls removed or disabled for DFT conflicts.
- `create_missing_dft` now creates the evidence span needed for a later human verify to pass the export safety gate.
- End-to-end DFT lifecycle verification covering issue creation, primary repair, human verify/reject, issue close, export safety, RAG, stale issues, and idempotency.

## Migration Notes

Deploy these migrations before enabling the integrated DFT lifecycle in an environment:

- `literature-ai/backend/app/migrations/003_project_library_v4_physical_tables.sql`
- `literature-ai/backend/app/migrations/004_dft_audit_issues.sql`

Migration `004_dft_audit_issues.sql` adds:

- `dft_audit_issues`
- indexes on `paper_id`, `status`, `issue_type`, and `target_id`
- a unique issue identity constraint over paper, target, issue type, and fingerprint

This release does not automatically clean historical duplicate DFT data and does not delete historical rows.

## Operator Checklist

Configure a separate primary repair MCP key:

```text
dft_primary_repair|DFT Primary Repair AI|<strong-random-key>|read_papers,repair_dft_issues
```

Do not grant `repair_dft_issues` by default to ordinary IDE AI, assigned audit AI, propose-only clients, human reviewer keys, or admin examples. Grant it only to a key intentionally used as the primary DFT repair role.

After deployment:

- restart backend services
- restart workers
- restart the MCP server process
- check `/api/system/agent-guide` for `mcp.capability_warnings`
- check `/api/settings/ide-prompts` for `mcp_capability_warnings`
- verify `GET /api/dft/audit-issues`
- verify `GET /api/dft/audit-report`

Any `repair_dft_issues_non_primary_repair_key` warning should be fixed before running DFT issue repair.

## User Behavior Changes

- The old review center is no longer a DFT final-truth adjudication entry.
- DFT conflicts should go through the DFT audit center or the paper DFT detail page.
- "Accept AI adjudication", "batch AI auto-advance", and ambiguous "use none" actions are not valid for DFT final truth.
- DFT audit issues with real `dft_results` targets link to the paper detail DFT tab.
- Issues with `target_id="new"`, empty targets, or source-scope errors must not show fake DFT detail links.
- Primary repair AI output still needs human review before export, RAG use, or ML-ready inclusion.

## Known Non-Blocking Risks

- Historical duplicate DFT data is not automatically cleaned.
- `EvidenceSpan.text` for repaired missing DFT rows can currently fall back to `row.evidence_text` or the repair reason. A future tightening can require explicit `quoted_text` or `evidence_text`.
- A real sample or sandbox-library rehearsal is still recommended before production rollout.
- Supporting-reference records, missing PDF evidence, duplicate merge choices, and concurrent repair attempts should continue to be monitored through read-only reports.

## Suggested Historical Data Follow-Up

Use read-only reporting before any historical cleanup:

- count historical duplicate DFT rows
- count stale DFT audit issues
- count `source_scope_error` issues
- count repair audit warnings
- list `fixed_by_primary_ai` rows awaiting human review
- list rejected rows that still have historical safe reviews and confirm the safety gate blocks them

Do not automatically delete, merge, verify, or reject historical DFT rows as part of this release.

## Final Test Matrix

The final integration readiness pass should include:

```powershell
cd D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend

docker exec literature-ai-backend-1 python -m pytest -q tests/test_dft_lifecycle_e2e.py
docker exec literature-ai-backend-1 python -m pytest -q tests/test_dft_audit_issue_service.py tests/test_dft_audit_issue_repair_service.py tests/test_dft_audit_report_service.py
docker exec literature-ai-backend-1 python -m pytest -q tests/test_verification_sessions.py tests/test_mcp_server.py tests/test_settings_api_access.py tests/test_ide_prompt_service.py
docker exec literature-ai-backend-1 python -m pytest -q tests/test_export_safety_gate.py tests/test_dft_review_display_status.py tests/test_review_boundary_enforcement.py tests/test_writing_safety_gate.py tests/test_rag_eligibility.py
docker exec literature-ai-backend-1 python -m compileall -q app tests

cd D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\frontend

npm test -- smoke.spec.js --workers=1
npm test -- dft_audit_center.spec.js dft_detail_deeplink.spec.js review_center_conflict_modal.spec.js dft_ml_dataset.spec.js

cd D:\Desktop\03_代码与开发\AI-shujvku

git diff --check
git status --short
```

The readiness branch that preceded this final integration had already reached:

- DFT backend lifecycle e2e (`tests/test_dft_lifecycle_e2e.py`): 3 passed
- DFT audit/repair/report (`test_dft_audit_issue_service.py`, `test_dft_audit_issue_repair_service.py`, `test_dft_audit_report_service.py`): 32 passed
- verification/MCP/settings/prompts (`test_verification_sessions.py`, `test_mcp_server.py`, `test_settings_api_access.py`, `test_ide_prompt_service.py`): 111 passed
- export/display/boundary/writing/RAG (`test_export_safety_gate.py`, `test_dft_review_display_status.py`, `test_review_boundary_enforcement.py`, `test_writing_safety_gate.py`, `test_rag_eligibility.py`): 81 passed
- backend `compileall`: passed
- frontend smoke (`smoke.spec.js --workers=1`): 152 passed
- DFT frontend focused tests (`dft_audit_center.spec.js`, `dft_detail_deeplink.spec.js`, `review_center_conflict_modal.spec.js`, `dft_ml_dataset.spec.js`): 23 passed
- `git diff --check`: passed
