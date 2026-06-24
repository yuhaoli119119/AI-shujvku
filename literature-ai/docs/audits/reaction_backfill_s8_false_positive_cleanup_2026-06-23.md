# Reaction Backfill S8 False Positive Cleanup

Date: 2026-06-23

Scope: corrective cleanup after deterministic reaction backfill surfaced a false positive `SRR_LiS` classification.

## Finding

Record `6fe590d8-4e0f-446d-89f4-af7a403d4b62` had `adsorbate = S8` and was initially classified as `SRR_LiS` by the rule backfill. Inspection showed the evidence came from a CO2RR paper and the text contained `Figure S8`, so plain `S8` was not a reliable Li-S intermediate signal.

The record was not exported to v3 ML because the existing safety gate blocked it:

- `review_status`: `missing`
- `review_gate_status`: `blocked`
- `provenance_level`: `text_evidence_only`
- `locator_status`: `text_only`
- `catalyst_sample_id`: missing

## Code Fix

`classify_reaction_record` was tightened so plain `S8` alone is no longer an SRR_LiS-specific signal. Li2Sx/Li2S intermediates remain SRR-specific, and contextual `S8` can still classify as SRR_LiS when Li-S/polysulfide context is present.

Regression test added:

- `test_plain_s8_without_lithium_sulfur_context_is_not_srr_specific`

Validation:

- `pytest tests/test_reaction_taxonomy.py tests/test_dft_reaction_backfill_apply.py tests/test_dft_reaction_backfill.py tests/test_dft_ml_dataset_v3.py tests/test_dft_ml_dataset_v3_api.py -q`
- Result: `47 passed`

## Data Cleanup

Only the 5 rule-generated reaction fields were cleared for the false-positive row:

- `reaction_type`
- `reaction_type_source`
- `reaction_type_confidence`
- `reaction_profile_version`
- `reaction_validation_status`

Guard used:

```sql
where id = '6fe590d8-4e0f-446d-89f4-af7a403d4b62'
  and reaction_type = 'SRR_LiS'
  and reaction_type_source = 'rule'
```

Original DFT fields were unchanged:

- `value`: `-3.71`
- `unit`: `eV`
- `property_type`: `adsorption_energy`
- `adsorbate`: `S8`
- `evidence_text`: unchanged

## Post-Cleanup State

Live DB row count stayed `668`.

Non-empty reaction classifications now total `6`:

- `HER`: 2 rule/valid rows
- `ORR`: 4 rule/valid rows
- `SRR_LiS`: 0 rule/valid rows

Backfill dry-run after cleanup:

- `eligible_updates`: 0
- `skipped_existing`: 6
- `writes_performed`: 0

v3 live export remains runnable but has no SRR_LiS training rows:

- `adsorption_energy`: `candidate_count=0`, `csv_rows=0`
- `reaction_barrier`: `candidate_count=0`, `csv_rows=0`

Conclusion: current live data is insufficient for SRR_LiS tabular ML training. The next useful work is either more SRR_LiS data ingestion/review or a review workflow for ambiguous Li-S candidates, not model training.
