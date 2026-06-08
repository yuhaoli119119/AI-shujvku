# Failure Recovery Acceptance

FAILURE_RECOVERY_ACCEPTANCE=PASS

- Created at: 2026-06-08T18:23:29.574306+00:00
- Library: `chain_failure_recovery_acceptance_20260608_001`
- API base: `http://localhost:8000`
- Restore attempted: `True`
- Restore succeeded: `True`
- Verified pollution: `False`
- Safe verified pollution: `False`

## Cases

| Case | Status | Root Cause | Reason |
| --- | --- | --- | --- |
| case_api_unreachable | PASS | api_server_unreachable |  |
| case_database_unreachable | PASS | database_unreachable |  |
| case_invalid_pdf_source | PASS | invalid_pdf_source |  |
| case_invalid_pdf_content | PASS | artifact_precondition_failed |  |
| case_artifact_file_missing | PASS | artifact_precondition_failed |  |
| case_external_audit_import_blocked | PASS | external_audit_blocked_by_artifact_precondition |  |

## Legacy Gate

- ACCEPTANCE_GATE: `PASS`
- root_cause: `None`

