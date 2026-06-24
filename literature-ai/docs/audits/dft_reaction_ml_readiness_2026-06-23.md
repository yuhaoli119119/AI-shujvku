# DFT Reaction ML Readiness Audit

Date: 2026-06-23

Scope: read-only audit of the current live PostgreSQL database for SRR_LiS tabular ML readiness. No database migration, backfill, DML, DDL, or safe_verified/verified update was performed.

## Commands Used

```powershell
Get-Content -Path 'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\docs\plans\tabular_ml_reaction_profiles_rollout_plan.md' -Encoding UTF8
git status --short
git log -1 --oneline
git branch -vv
docker compose ps --format json
docker compose exec -T backend python tools/audit_reaction_backfill.py --sample-limit 20
docker compose exec -T backend python tools/audit_reaction_backfill.py --sample-limit 20 | Set-Content -Path 'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\docs\audits\dft_reaction_backfill_dryrun_2026-06-23.json' -Encoding UTF8
```

Additional read-only Python snippets were run inside the backend container to call `build_dft_ml_dataset_v3`, `build_dft_ml_dataset_v3_csv`, and to inspect SRR_LiS dry-run candidates via SELECT-only SQL.

## Repository State

- HEAD: `b303d6e4 Fix embedding dimension contract`
- Branch: `codex/library-safety-hardening`, ahead of origin by 1 commit.
- Worktree was already dirty before this audit, with Stage 1-6 implementation files present as uncommitted changes.

## Reaction Backfill Dry-Run

Full dry-run output was saved to:

`docs/audits/dft_reaction_backfill_dryrun_2026-06-23.json`

Key output:

```json
{
  "dry_run": true,
  "writes_performed": 0,
  "profile_version": "reaction_profiles_v1",
  "total_records": 668,
  "classifiable": 7,
  "ambiguous": 628,
  "unsupported": 33,
  "unchanged_human_labels": 0,
  "by_reaction_type": {
    "HER": 13,
    "OER": 2,
    "ORR": 20,
    "SRR_LiS": 5,
    "UNKNOWN": 628
  },
  "by_property_type": {
    "adsorption_energy": 2,
    "gibbs_free_energy_change": 5
  },
  "by_reaction_and_property": {
    "HER:gibbs_free_energy_change": 2,
    "ORR:adsorption_energy": 1,
    "ORR:gibbs_free_energy_change": 3,
    "SRR_LiS:adsorption_energy": 1
  }
}
```

Schema status from the dry-run:

```json
{
  "reaction_columns_present": [],
  "reaction_columns_missing": [
    "reaction_type",
    "reaction_type_source",
    "reaction_type_confidence",
    "reaction_profile_version",
    "reaction_validation_status"
  ]
}
```

## SRR_LiS Candidate Inspection

The dry-run classified 5 records as `SRR_LiS`:

| record_id | property_type | adsorbate | value | unit | validator status | validator reasons |
| --- | --- | --- | ---: | --- | --- | --- |
| `11b5e80f-b252-49ef-bee1-c065bcc186f4` | `migration_barrier` | `Stone-Wales` | 0.27 | eV | `out_of_scope` | `intermediate_out_of_scope` |
| `1540c09a-379f-4d1c-8a90-dff9e8e7b74c` | `reaction_barrier` | `Stone-Wales` | 0.27 | eV | `out_of_scope` | `intermediate_out_of_scope`, `property_out_of_scope` |
| `1a64c80b-f2af-44aa-80c1-d3bd367c1a35` | `reaction_barrier` | `graphene` | 0.01 | eV | `out_of_scope` | `intermediate_out_of_scope`, `property_out_of_scope` |
| `2e130508-1f2e-4521-9b53-90a10249ad8d` | `reaction_barrier` | `graphene` | 2.0 | eV | `out_of_scope` | `intermediate_out_of_scope`, `property_out_of_scope` |
| `6fe590d8-4e0f-446d-89f4-af7a403d4b62` | `adsorption_energy` | `S8` | -3.71 | eV | `valid` | none |

Approximate SRR_LiS dry-run candidate count: 5.

Approximate SRR_LiS validator-ready count before DB reaction backfill: 1, and it is `adsorption_energy`.

## v3 Dataset / CSV Readiness

Attempted service calls:

```python
build_dft_ml_dataset_v3(session, task="adsorption_energy", ready_only=False)
build_dft_ml_dataset_v3_csv(session, task="adsorption_energy")
build_dft_ml_dataset_v3(session, task="reaction_barrier", ready_only=False)
build_dft_ml_dataset_v3_csv(session, task="reaction_barrier")
```

Current result: blocked by live schema mismatch. The current ORM/service code expects `dft_results.reaction_type` and related columns, but the live database table does not have them yet.

Primary error:

```text
ProgrammingError: column dft_results.reaction_type does not exist
```

Therefore current v3 manifest fields could not be produced from the live service for either task:

| task | candidate_count | task_candidate_count | returned_count | label_ready_count | tabular_ready_count | excluded_counts |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `adsorption_energy` | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable; schema missing reaction columns |
| `reaction_barrier` | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable; schema missing reaction columns |

## Baseline Status

No v3 CSV could be generated against the current live DB, so `tools/ml_baseline_srr_lis.py` was not run on live exported data.

Baseline status:

| task | status | reason |
| --- | --- | --- |
| `adsorption_energy` | skipped | v3 CSV generation failed because live DB lacks reaction columns |
| `reaction_barrier` | skipped | v3 CSV generation failed because live DB lacks reaction columns |

## Conclusion

Current best first target is `adsorption_energy`, but only as a very small proof-of-plumbing target after the reaction schema exists and the read-only v3 export can run. The current live DB does not yet support v3 ML export because the reaction columns are missing, and the dry-run only finds 1 SRR_LiS validator-valid adsorption-energy record.

`reaction_barrier` is not ready in the current data snapshot. The dry-run sees 3 SRR_LiS-like barrier records, but all are rejected by the current SRR_LiS validator because the intermediates/properties are out of task scope.

Practical decision for this snapshot: current data is insufficient for meaningful SRR_LiS tabular ML training. If forced to choose an initial target after schema migration/backfill, choose `adsorption_energy` first.
