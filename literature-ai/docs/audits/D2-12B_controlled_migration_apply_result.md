# D2-12B Controlled Migration Apply Result

## Execution Summary

- apply time: `2026-05-26T12:26:32Z`
- local wall-clock confirmation: `2026-05-26T20:30:44+08:00`
- commit before apply: `213686064dac2daea4f9ae37f17adb3f3d9c85c1`
- apply command: `python scripts/d2_controlled_historical_mirror_migration.py --apply`
- apply executed: `true`
- registry updated: `true`
- copied files count: `26`
- copied files hash verification: `passed`
- hash mismatches: `0`

## Roots

- source root before apply: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\backend\DDesktop代码开发AI检索数据库literature-aibackenddatalibrariesdefault`
- target root after apply: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default`
- source historical mirror root still exists after apply: `true`
- canonical registry switched to target root: `true`

## Registry And SQLite SHA256

- canonical registry SHA256 before apply: `40d3073efffda894fa6d1016747b6086df6a8b87878d8e3d7ace0d19899afb01`
- canonical registry SHA256 after apply: `a883d76761487cdc431b313e8d150d27e6449fdd903a4fc312ea66b01cff7812`
- active SQLite SHA256 before apply: `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df`
- active SQLite SHA256 after apply: `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df`
- target `database.sqlite` exists: `true`
- target `library.json` exists: `true`

## Copy Result

- copied active database: `1`
- copied required library metadata: `1`
- copied DB-referenced artifacts: `24`
- DB-referenced artifacts by type: `pdf=6`, `markdown=6`, `tei=6`, `docling_json=6`
- unreferenced files copied by migration apply: `false`
- skipped unreferenced files at apply time: `222`
- migration audit JSON: `D:\Desktop\03_代码与开发\AI-shujvku\backups\d2_controlled_historical_mirror_migration\20260526T122632Z\migration_audit_report.json`
- migration audit markdown: `D:\Desktop\03_代码与开发\AI-shujvku\backups\d2_controlled_historical_mirror_migration\20260526T122632Z\migration_audit_report.md`

## Copied File SHA256 Summary

- `database.sqlite`: `a1c7fb7b7a3b1beba57a532b994a8dd14d306c4b67f3a8fe842ad74fd73852df`
- `library.json`: `2d4a01159f6bc452c757cd7cf50d099efd3abdab52eef2b53ed961365cce6cd9`
- DB-referenced artifacts: `24` source/target SHA256 pairs matched exactly
- target copied-file hash mismatches: `0`

## Post-Apply Active DB Proof

- active DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- db kind: `sqlite`
- active database papers total: `15`
- recovered from candidate scan: `false`
- effective DB matches active library DB path: `true`

## Post-Apply Coverage Proof

`python scripts/d2_real_extraction_coverage_gate.py` reported:

- `mode=dry_run`
- `db_kind=sqlite`
- `effective_db_path=D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `papers_total=15`
- `extractable_papers_count=1`
- `extractable_dft_results_count=1`
- `extractable_property_types=['reaction_barrier']`
- evidence preview includes `Li2S2`, `reaction_barrier`, and `2.73 eV`
- `apply_executed=False`
- `export_writing_gate_unchanged=True`

## Post-Apply Export And Writing Safety Proof

`python scripts/audit_ai_workflow_boundary.py` reported:

- `active_db_kind=sqlite`
- `active_db_path=D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- `is_active_library_sqlite=True`
- `external_analysis_runs=0`
- `verified_reviews=0`
- `dft_export_total_candidates=0`
- `dft_export_safe_eligible=0`
- `writing_cards_total=6`
- `writing_cards_safe_usable=0`

## Rollback Instructions

- registry backup path: `D:\Desktop\03_代码与开发\AI-shujvku\backups\d2_controlled_historical_mirror_migration\20260526T122632Z\library_registry.json.bak`
- restore the canonical registry from that backup if runtime validation must be reverted
- remove only files listed in the migration copy plan from the target root if target writes must be rolled back
- rerun the controlled migration dry-run and active DB audit after rollback

## Explicit Non-Actions

- historical mirror root was not deleted
- full source root was not copied
- unreferenced PDFs and figures were not copied by the migration apply script
- no verified review was written
- no extraction apply was executed
- no force push was performed

## Post-Test Residual Observation

After post-apply `pytest`, the target active library contains `10` unreferenced UUID-only PDFs sized `15-23` bytes. These files are not in the migration apply copy plan and were not part of the `26` copied files verified by the apply audit.

Post-apply target-conflict/readiness gates are still pre-apply oriented:

- `d2_target_conflict_and_artifact_inventory_gate.py` reports `target_conflicts_count=2` because the target now intentionally contains `database.sqlite` and `library.json`
- `d2_controlled_historical_mirror_migration.py --dry-run` now reports `ready_for_apply=false` because the canonical registry is already on the target root
