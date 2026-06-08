# Internal Library Cleanup Plan

INTERNAL_LIBRARY_CLEANUP_PLAN=READY_FOR_USER_CONFIRMATION

## Summary

- total_papers: 30
- candidate_delete_papers: 0
- candidate_delete_libraries: 0
- manual_review_papers: 0
- protected_papers: 30
- protected_libraries: 1
- storage_files_to_delete_count: 0
- storage_bytes_to_delete: 0
- related_external_analysis_records: 0
- related_review_records: 0

## Backup Required Before Delete

1. Export target DB records to JSON.
2. Export target paper_id list.
3. Export target artifact path list.
4. Optionally run pg_dump before any delete operation.

- backup_manifest: reports\internal_library_cleanup_backup_manifest_after.json
- paper_ids_output: reports\internal_library_cleanup_paper_ids_after.txt
- artifact_paths_output: reports\internal_library_cleanup_artifact_paths_after.txt

## Candidate Delete Libraries

_None._

## Protected Libraries

| library_name | papers |
|---|---:|
| 石墨炔 | 30 |

## Manual Review Libraries

_None._

## Candidate Papers

_None._

## Safety Notes

- This report is a dry-run plan only.
- No DELETE SQL was executed.
- No storage artifact files were removed.
- No git staging or commit was performed by this script.
