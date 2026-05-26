# D2-12D Post-Migration Audit Semantics

Date: 2026-05-26

## Why This Gate Was Needed

D2-12B.1 completed the controlled migration from the historical mirror root to the canonical target root. After that point, `database.sqlite` and `library.json` under `literature-ai/data/libraries/default` are no longer pre-migration conflicts; they are expected active files.

Before this gate, post-migration scripts still used pre-migration semantics:

- The target conflict gate reported active target `database.sqlite` and `library.json` as conflicts.
- The controlled migration dry-run reported `ready_for_apply=false` without clearly stating that the migration was already complete.
- The active-library test pollution audit did not accept an explicit `--dry-run` argument or expose a direct regression field.

## Modified Files

- `literature-ai/backend/scripts/d2_target_conflict_and_artifact_inventory_gate.py`
- `literature-ai/backend/scripts/d2_controlled_historical_mirror_migration.py`
- `literature-ai/backend/scripts/d2_active_library_test_pollution_audit.py`
- `literature-ai/backend/tests/test_d2_target_conflict_and_artifact_inventory_gate.py`
- `literature-ai/backend/tests/test_d2_controlled_historical_mirror_migration.py`
- `literature-ai/backend/tests/test_d2_active_library_test_pollution_audit.py`
- `literature-ai/docs/audits/D2-12D_post_migration_audit_semantics.md`

## New Semantics

Target conflict gate:

- `migration_phase=post_migration`
- `target_root_status=active_canonical_target`
- `expected_active_files_count=2`
- `raw_target_conflicts_count=2`
- `target_conflicts_count=0`

The pre-migration behavior is preserved by tests: if the canonical registry still points at the historical mirror root and the target root contains `database.sqlite` or `library.json`, they remain conflicts.

Controlled migration dry-run:

- `migration_phase=post_migration`
- `already_migrated=true`
- `migration_complete=true`
- `apply_should_run=false`
- `ready_for_apply=false`
- `ready_for_apply_reason=already_migrated`
- `post_migration_hashes_ok=true`
- `post_migration_hash_mismatches_count=0`

The dry-run no longer treats the completed migration state as an apply blocker. Hash mismatch and missing-target variants are still tested as incomplete/high-risk states.

Test pollution audit:

- Explicit `--dry-run` is accepted.
- `tiny_uuid_only_unref_pdf_count` is emitted.
- `pollution_detected=false` when the count is zero.
- Cleanup still requires `--cleanup --apply` and does not run by default.

## Post-Migration Proof

Active DB proof:

- Active DB path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- Active DB kind: `sqlite`
- Active database papers total: `15`
- Recovered from candidate scan: `false`

Target gate proof:

- `migration_phase=post_migration`
- `target_root_status=active_canonical_target`
- `expected_active_files_count=2`
- `target_conflicts_count=0`

Controlled dry-run proof:

- `already_migrated=true`
- `migration_complete=true`
- `apply_should_run=false`
- `ready_for_apply=false`
- `ready_for_apply_reason=already_migrated`
- `copy_operations_count=26`
- `db_referenced_artifacts_count=24`
- `includes_unreferenced_files=false`

Pollution regression proof after full pytest:

- `pdf_files_count=6`
- `unreferenced_pdf_count=0`
- `tiny_uuid_only_unref_pdf_count=0`
- `pollution_detected=false`

Coverage proof:

- `Li2S2 / reaction_barrier / 2.73 eV` remains discoverable.
- `apply_executed=false`

Export and writing safety proof:

- `dft_export_safe_eligible=0`
- `writing_cards_safe_usable=0`
- `verified_reviews=0`

## Verification

- `python -m compileall app findpapers tests`: passed
- `python -m pytest -q`: `300 passed, 457 warnings`

## Explicit Non-Actions

- Did not execute migration `--apply`.
- Did not delete the historical mirror root.
- Did not delete DB-referenced artifacts.
- Did not copy a full source root.
- Did not modify active SQLite contents.
- Did not write a verified review.
- Did not execute extraction apply.
- Did not force push.

## Remaining Risk

The readiness script itself still exposes historical migration-era fields such as `source_root_is_historical_mirror=false` and a pre-migration `recommended_next_gate`. D2-12D resolves the phase-aware semantics in the target conflict gate and controlled dry-run, but future cleanup could also make the readiness script directly phase-aware to reduce operator confusion.
