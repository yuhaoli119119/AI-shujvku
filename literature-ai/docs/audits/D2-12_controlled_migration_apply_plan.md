# D2-12 Controlled Historical Mirror Migration Apply Plan + Script Hardening

## Scope

This round only delivers the controlled migration apply plan, root-level script wrappers, safety hardening, tests, and audit documentation.

No migration apply was executed in this round.

## What Was Added / Hardened

- added backend controlled migration script:
  `literature-ai/backend/scripts/d2_controlled_historical_mirror_migration.py`
- added root wrappers so the scripts can be run from the repository root:
  - `scripts/d2_historical_mirror_migration_readiness.py`
  - `scripts/d2_target_conflict_and_artifact_inventory_gate.py`
  - `scripts/d2_controlled_historical_mirror_migration.py`
- hardened readiness / gate reports with explicit count fields used by the apply gate
- added focused tests for:
  - default dry-run no-write behavior
  - referenced-only copy plan scope
  - target conflict blocking
  - missing referenced artifact blocking
  - registry update ordering
  - registry rollback helper

## Controlled Apply Guarantees

The new script is dry-run by default.

Without `--apply`, it must not write files.

With `--apply`, it must re-check all of the following before writing:

- `target_conflicts_count == 0`
- active DB kind is SQLite
- `active_database_papers_total == 15`
- `recovered_from_candidate_scan == false`
- source root is still the current historical mirror root
- target root is still `literature-ai/data/libraries/default`
- canonical registry still points to the source root
- missing DB-referenced artifacts count is `0`
- duplicate artifact paths count is `0`

The copy scope is constrained to:

- `database.sqlite`
- required library metadata
- DB-referenced artifacts only

The script must exclude:

- unreferenced PDFs
- unreferenced figures
- full source-root copy

The script also enforces registry update ordering:

- copy first
- hash verification second
- canonical registry update only after copy + hash verification succeed
- post-update runtime validation after registry change
- registry restore from backup if post-update validation fails

## Execution Status For This Round

- apply executed: `false`
- canonical registry changed: `false`
- active SQLite moved: `false`
- real artifacts copied: `false`
- real data/artifacts deleted: `false`
- verified review written: `false`

## Remaining Risk

- actual `--apply` has not been exercised in the live environment yet
- runtime activation after registry cutover still depends on the current library manager / DB switching behavior
- the migration remains intentionally blocked until the live dry-run outputs are revalidated immediately before any future apply
