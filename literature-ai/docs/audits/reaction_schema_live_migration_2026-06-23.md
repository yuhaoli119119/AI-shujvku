# Reaction Schema Live Migration Audit

Date: 2026-06-23

Scope: minimal additive schema migration on the current live PostgreSQL database so v3 DFT ML export can reach manifest/CSV generation. No data backfill, data value write, verification status change, broad init_db migration, code change, test change, frontend change, MCP change, or notebook change was performed.

## Commands Used

```powershell
Get-Content -Path 'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\docs\plans\tabular_ml_reaction_profiles_rollout_plan.md' -Encoding UTF8
git status --short
git log -1 --oneline
git branch -vv
```

Migration was executed inside the backend container with SQLAlchemy `create_engine(...)` and `text(...)`. The only DDL statements executed were:

```sql
ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type VARCHAR(32);
ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type_source VARCHAR(32);
ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type_confidence FLOAT;
ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_profile_version VARCHAR(64);
ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_validation_status VARCHAR(32);
CREATE INDEX IF NOT EXISTS ix_dft_results_reaction_type ON dft_results (reaction_type);
```

Post-migration validation commands:

```powershell
docker compose exec -T backend python tools/audit_reaction_backfill.py --sample-limit 20
```

Additional read-only Python snippets were run inside the backend container to call:

```python
build_dft_ml_dataset_v3(session, task="adsorption_energy", ready_only=False)
build_dft_ml_dataset_v3_csv(session, task="adsorption_energy")
build_dft_ml_dataset_v3(session, task="reaction_barrier", ready_only=False)
build_dft_ml_dataset_v3_csv(session, task="reaction_barrier")
```

## Migration Snapshot

| Check | Before | After |
| --- | ---: | ---: |
| `SELECT count(*) FROM dft_results` | 668 | 668 |
| row count unchanged | yes | yes |
| `ix_dft_results_reaction_type` exists | false | true |

Reaction columns before:

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

Reaction columns after:

```json
{
  "reaction_columns_present": [
    "reaction_type",
    "reaction_type_source",
    "reaction_type_confidence",
    "reaction_profile_version",
    "reaction_validation_status"
  ],
  "reaction_columns_missing": []
}
```

## Reaction Backfill Dry-Run After Migration

The dry-run completed without writes:

```json
{
  "dry_run": true,
  "writes_performed": 0,
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

Dry-run schema section after migration:

```json
{
  "reaction_columns_missing": [],
  "reaction_columns_present": [
    "reaction_type",
    "reaction_type_source",
    "reaction_type_confidence",
    "reaction_profile_version",
    "reaction_validation_status"
  ],
  "selected_columns": [
    "id",
    "property_type",
    "adsorbate",
    "reaction_step",
    "evidence_text",
    "reaction_type",
    "reaction_type_source",
    "reaction_type_confidence",
    "reaction_profile_version",
    "reaction_validation_status"
  ]
}
```

## v3 Live Export Validation

The v3 JSON and CSV builders now run against the live DB. Because this batch intentionally did not write any `reaction_type` values, all v2 source candidates are excluded as `unknown_reaction_type`.

| Task | JSON source_candidate_count | JSON candidate_count | JSON task_candidate_count | JSON returned_count | JSON label_ready_count | JSON tabular_ready_count | JSON excluded_counts | CSV ready-only data rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `adsorption_energy` | 236 | 0 | 0 | 0 | 0 | 0 | `{"unknown_reaction_type": 236}` | 0 |
| `reaction_barrier` | 236 | 0 | 0 | 0 | 0 | 0 | `{"unknown_reaction_type": 236}` | 0 |

CSV output contained one non-empty line for each task, meaning the header row was produced and no training-ready data rows were returned.

## Conclusion

The live database schema blocker is removed. v3 live export now reaches manifest and CSV generation for both `adsorption_energy` and `reaction_barrier`.

No reaction classification values were written, so v3 task datasets remain empty until an explicit, separately authorized reaction backfill writes `reaction_type` and related fields. The DFT row count stayed unchanged at 668.
