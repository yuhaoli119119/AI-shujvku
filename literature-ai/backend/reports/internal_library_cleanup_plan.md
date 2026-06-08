# Internal Library Cleanup Plan

INTERNAL_LIBRARY_CLEANUP_PLAN=READY_FOR_USER_CONFIRMATION

## Summary

- total_papers: 67
- candidate_delete_papers: 37
- candidate_delete_libraries: 15
- manual_review_papers: 0
- protected_papers: 30
- protected_libraries: 1
- storage_files_to_delete_count: 840
- storage_bytes_to_delete: 244744510
- related_external_analysis_records: 62
- related_review_records: 0

## Backup Required Before Delete

1. Export target DB records to JSON.
2. Export target paper_id list.
3. Export target artifact path list.
4. Optionally run pg_dump before any delete operation.

- backup_manifest: reports\internal_library_cleanup_backup_manifest.json
- paper_ids_output: reports\internal_library_cleanup_paper_ids.txt
- artifact_paths_output: reports\internal_library_cleanup_artifact_paths.txt

## Candidate Delete Libraries

| library_name | papers |
|---|---:|
| chain_failure_recovery_acceptance_20260608 | 1 |
| chain_failure_recovery_acceptance_20260608_001 | 2 |
| chain_fresh_realpaper_acceptance_20260608 | 3 |
| chain_fresh_repeatability_20260608_round01 | 3 |
| chain_fresh_repeatability_20260608_round02 | 4 |
| chain_fresh_repeatability_20260608_round03 | 3 |
| chain_realpaper_smoke_20260608 | 3 |
| chain_smoke_20260608 | 3 |
| core_suite_failure_recovery_20260608 | 1 |
| core_suite_failure_recovery_20260608_probe | 2 |
| core_suite_fresh_chain_repeatability_20260608_round01 | 3 |
| core_suite_fresh_chain_repeatability_20260608_round02 | 3 |
| core_suite_fresh_chain_repeatability_20260608_round03 | 1 |
| core_suite_fresh_realpaper_chain_20260608 | 2 |
| fresh_real_paper_smoke_20260608 | 3 |

## Protected Libraries

| library_name | papers |
|---|---:|
| 石墨炔 | 30 |

## Manual Review Libraries

_None._

## Candidate Papers

| paper_id | library_name | external_runs | candidates | reviews | artifact_files | risk | reason |
|---|---|---:|---:|---:|---:|---|---|
| 041818be-4d57-482c-be82-3bed9718fe07 | chain_failure_recovery_acceptance_20260608 | 0 | 0 | 0 | 18 | low | library_prefix_chain |
| 3088a59e-36d6-4aca-a390-c3bd543e8aca | chain_failure_recovery_acceptance_20260608_001 | 0 | 0 | 0 | 18 | low | library_prefix_chain |
| b002cdda-b5f8-4c5e-ad71-c5085b5cda8c | chain_failure_recovery_acceptance_20260608_001 | 1 | 0 | 0 | 32 | medium | library_prefix_chain |
| f1650377-6497-4e59-8039-b2aca2162402 | chain_fresh_realpaper_acceptance_20260608 | 1 | 1 | 0 | 28 | medium | library_prefix_chain |
| 085011c1-b4bd-459e-9f19-3cdfa030ceb7 | chain_fresh_realpaper_acceptance_20260608 | 1 | 1 | 0 | 39 | medium | library_prefix_chain |
| 783ab318-a4b7-44cb-a6d0-87db769e3a4f | chain_fresh_realpaper_acceptance_20260608 | 1 | 1 | 0 | 30 | medium | library_prefix_chain |
| 6efe53f3-5c28-4b36-8ebf-3bf9426087a5 | chain_fresh_repeatability_20260608_round01 | 1 | 1 | 0 | 18 | medium | library_prefix_chain |
| 76590237-aecc-4613-a456-a21fbd5e3d03 | chain_fresh_repeatability_20260608_round01 | 1 | 1 | 0 | 40 | medium | library_prefix_chain |
| be9916fe-3769-4835-98c8-fb9770f6d1b4 | chain_fresh_repeatability_20260608_round01 | 1 | 1 | 0 | 23 | medium | library_prefix_chain |
| c8b9e986-eb06-4475-827b-417f093121e5 | chain_fresh_repeatability_20260608_round02 | 0 | 0 | 0 | 7 | low | library_prefix_chain |
| 4342baad-cda9-414f-a8c4-5eafcc97c62f | chain_fresh_repeatability_20260608_round02 | 1 | 1 | 0 | 22 | medium | library_prefix_chain |
| 895ebcfc-5067-434d-bb73-5a1592006059 | chain_fresh_repeatability_20260608_round02 | 1 | 1 | 0 | 41 | medium | library_prefix_chain |
| f3105ef3-39e8-497d-9fc4-272d17100419 | chain_fresh_repeatability_20260608_round02 | 1 | 1 | 0 | 26 | medium | library_prefix_chain |
| c20187e0-c277-49d1-8f44-b8c7380813dc | chain_fresh_repeatability_20260608_round03 | 1 | 1 | 0 | 40 | medium | library_prefix_chain |
| 7041a455-2bdc-40ae-a65e-bb891decce9b | chain_fresh_repeatability_20260608_round03 | 1 | 1 | 0 | 26 | medium | library_prefix_chain |
| 10204832-d7d4-4cc8-81a9-b0a47024393b | chain_fresh_repeatability_20260608_round03 | 1 | 1 | 0 | 30 | medium | library_prefix_chain |
| e636ff33-55fc-436d-b4ec-1b4f064f4050 | chain_realpaper_smoke_20260608 | 1 | 1 | 0 | 29 | medium | library_prefix_chain |
| d5d5c467-8a91-4f9a-9c93-4e4c84a30bab | chain_realpaper_smoke_20260608 | 1 | 1 | 0 | 20 | medium | library_prefix_chain |
| 2d977b15-7715-4a27-87e3-985dc77c4da1 | chain_realpaper_smoke_20260608 | 1 | 1 | 0 | 23 | medium | library_prefix_chain |
| 8019ffef-fce7-4183-96cf-27a37bb6452c | chain_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_chain |
| 0a8144dc-5728-4f05-a641-489858af486b | chain_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_chain |
| 5060875c-5615-41b4-aa74-053b8721ec7f | chain_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_chain |
| 8073cae7-65c8-44ed-93e9-3b055d6c00e7 | core_suite_failure_recovery_20260608 | 0 | 0 | 0 | 18 | low | library_prefix_core_suite |
| f1181561-dcdd-47ce-9738-44ed1cbf851a | core_suite_failure_recovery_20260608_probe | 0 | 0 | 0 | 18 | low | library_prefix_core_suite |
| c960f2d3-ed97-4aa2-b22d-98dbc98f684a | core_suite_failure_recovery_20260608_probe | 1 | 0 | 0 | 28 | medium | library_prefix_core_suite |
| 3e786729-1226-4a63-9e2b-acef1eee09cb | core_suite_fresh_chain_repeatability_20260608_round01 | 1 | 1 | 0 | 24 | medium | library_prefix_core_suite |
| 94c51100-1857-4e4e-b3fb-e076269f9c9c | core_suite_fresh_chain_repeatability_20260608_round01 | 1 | 1 | 0 | 22 | medium | library_prefix_core_suite |
| 822b9aef-b07e-4a42-98bb-64318b473774 | core_suite_fresh_chain_repeatability_20260608_round01 | 1 | 1 | 0 | 48 | medium | library_prefix_core_suite |
| 6933ba6c-e1aa-4133-8d6b-e47dfcc48dd8 | core_suite_fresh_chain_repeatability_20260608_round02 | 1 | 1 | 0 | 38 | medium | library_prefix_core_suite |
| efa16d42-add6-4d56-9adb-8b7d6321ec31 | core_suite_fresh_chain_repeatability_20260608_round02 | 1 | 1 | 0 | 24 | medium | library_prefix_core_suite |
| 297892ea-25a5-4b7a-8f93-88380e1b9948 | core_suite_fresh_chain_repeatability_20260608_round02 | 1 | 1 | 0 | 32 | medium | library_prefix_core_suite |
| c0d6973c-03f4-486f-af21-8254ef716e67 | core_suite_fresh_chain_repeatability_20260608_round03 | 1 | 1 | 0 | 28 | medium | library_prefix_core_suite |
| 185c98b5-f228-4877-b819-9ea5cc178199 | core_suite_fresh_realpaper_chain_20260608 | 1 | 1 | 0 | 22 | medium | library_prefix_core_suite |
| 59dc1871-1cd6-4442-81b3-36c7951ee2d0 | core_suite_fresh_realpaper_chain_20260608 | 1 | 1 | 0 | 28 | medium | library_prefix_core_suite |
| 316dc16c-d627-4ea2-98e2-b07079264e96 | fresh_real_paper_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_fresh |
| b1cd5066-81cb-49f9-8646-3ed443afd4cc | fresh_real_paper_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_fresh |
| cd4888b0-eebf-40c4-b112-e34df9cb3bed | fresh_real_paper_smoke_20260608 | 1 | 1 | 0 | 0 | medium | library_prefix_fresh |

## Safety Notes

- This report is a dry-run plan only.
- No DELETE SQL was executed.
- No storage artifact files were removed.
- No git staging or commit was performed by this script.
