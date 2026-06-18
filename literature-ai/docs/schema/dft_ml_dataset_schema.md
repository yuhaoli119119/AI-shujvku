# DFT ML Dataset Schema

This document defines the current export contract for:

```text
GET /api/papers/export/dft-dataset
```

Current export schema:
- `metadata.schema_version = "dft_results_ml_v2"`
- safety gate remains `safe_verified_with_required_evidence`

Machine-verifiable model:
- `backend/app/schemas/dft_export.py`
- main contract model: `DFTMLDatasetExportV2`
- consumer helper: `select_training_records_v2(...)`

Only records that pass the existing review/evidence/locator/material-identity gates are exported. This document explains the meaning of the exported fields so downstream LM/ML code does not accidentally over-trust paper-level context.

## Top-Level Shape

```json
{
  "metadata": {
    "dataset_version": "dft-ml-dataset-v0.2",
    "schema_version": "dft_results_ml_v2",
    "safety_gate": "safe_verified_with_required_evidence",
    "ml_setting_field": "linked_dft_setting"
  },
  "records": [],
  "lm_records": []
}
```

Top-level keys:
- `metadata`: export version, filters, safety summary, readiness summary
- `records`: numeric DFT rows intended for ML pipelines
- `lm_records`: non-numeric DFT claims intended for LM / text-assistance workflows only

## Metadata

Important metadata fields:

| Field | Meaning |
| --- | --- |
| `dataset_version` | Dataset packaging version. |
| `schema_version` | Current contract version. Downstream parsers should pin this. |
| `safety_gate` | Export gate name. Only gated-safe rows appear in `records` and `lm_records`. |
| `eligible_count` / `blocked_count` / `blocked_reasons` | Safety-gate audit summary across evaluated candidates. |
| `numeric_record_count` | Number of numeric exported records. |
| `numeric_ml_ready_count` | Numeric records with no `ml_blockers`. |
| `numeric_blocked_count` | Numeric exported records that remain not-ready for training. |
| `lm_record_count` | Number of LM-only records. |
| `ml_setting_field` | The only recommended DFT setting field for ML joins. This is currently always `linked_dft_setting`. |

## Numeric Records

Each item in `records` contains:
- `record_id`
- `paper`
- `target`
- `catalyst`
- `catalyst_candidates`
- `dft_settings`
- `paper_level_dft_settings`
- `linked_dft_setting`
- `setting_link_status`
- `setting_link_reason`
- `setting_link_candidates`
- `recommended_ml_setting_field`
- `descriptor_fields`
- `sample_context`
- `provenance`
- `ml_blockers`
- `ml_readiness_score`
- `is_ml_ready`

### Target Contract

Every numeric record must include these target fields:

| Field | Meaning |
| --- | --- |
| `property_type` | Original exported property label from the row. |
| `normalized_property_type` | Normalized internal property label. |
| `canonical_property_type` | Canonical ML/LM taxonomy label. |
| `property_family` | Higher-level bucket such as `energetics`, `kinetics`, `electronic_descriptor`. |
| `property_subtype` | More specific subtype retained for specialization. |
| `physical_dimension` | Canonical dimension such as `energy`, `charge`, `length`, `text`. |
| `ml_role` | One of `target`, `descriptor`, or `lm_auxiliary`. Numeric `records` only contain `target` / `descriptor`. |
| `adsorbate` / `canonical_adsorbate` | Raw and canonical adsorbate labels. |
| `value` / `unit` | Raw extracted numeric value and unit. |
| `normalized_value` / `normalized_unit` | Derived normalized value and unit when normalization is safe. |
| `normalization_status` | For example `normalized`, `identity`, `basis_qualified`, `unrecognized_unit`. |
| `normalization_blockers` | Unit-normalization blockers retained even after the row passes export safety gates. |

Rule:
- downstream ML must use `canonical_property_type`, `property_family`, `property_subtype`, `physical_dimension`, and `ml_role`
- downstream ML must not silently assume `normalized_value` exists
- if `normalized_value` or `normalized_unit` is absent, the row must carry an explicit blocker explaining why

### Setting Contract

There are three setting-related fields with different trust levels:

| Field | Trust level | Intended use |
| --- | --- | --- |
| `linked_dft_setting` | Highest | The only recommended ML setting field. Represents the result-level primary setting when a unique link exists. |
| `setting_link_candidates` | Audit only | Non-unique candidates when the result-level link is ambiguous. |
| `paper_level_dft_settings` | Audit / compatibility only | Paper-level settings pool. Not safe to treat as the unique training setting. |

Compatibility note:
- `dft_settings` is retained as a legacy compatibility alias of paper-level settings
- downstream training code must not infer “setting is clear” from `paper_level_dft_settings` or `dft_settings`
- `recommended_ml_setting_field` exists on every numeric record and currently points to `linked_dft_setting`
- `setting_link_reason` explains whether the link is singleton, heuristic, ambiguous, or missing

### Instance Keys

`sample_context` exposes three related scopes:

| Field | Meaning | Intended use |
| --- | --- | --- |
| `instance_key` | Most specific record-level instance scope. | Exact descriptor-to-target matching and instance-level joins. |
| `instance_anchor_key` | Same instance but without the final target-context suffix. | Conservative fallback grouping inside the same local reaction/adsorbate/material instance. |
| `material_scope_key` | Shared material/surface/setting scope without the adsorbate/reaction anchor. | Audit or descriptor ambiguity detection, not default ML feature joins. |
| `target_context_key` | Target/descriptor compatibility scope key. | Descriptor-to-target compatibility matching. |
| `instance_scope_level` | Scope label such as `target_context`, `instance_scope`, `material_scope`. | Explains how strict the descriptor scope is. |

Current `instance_key` composition includes:
- catalyst identity: `catalyst_sample_id`, otherwise material identity fallback
- `canonical_adsorbate`
- target-context key
- `reaction_step`
- `source_section`
- linked result setting id when unique, otherwise `setting_link_status`
- evidence-derived context when available:
  - `material_identity`
  - `material`
  - `structure_name`
  - `surface_facet`
  - `adsorption_site`
  - `coverage`
  - `slab`
  - `termination`

Descriptor assignment rule:
- `descriptor_fields` may only be attached from the same `instance_key`
- or from a clearly compatible descriptor scope with a unique compatible target
- if compatibility is not unique, the record is blocked by `descriptor_instance_ambiguous`

Additional `sample_context` counters may be present on numeric records:
- `numeric_record_count`
- `target_record_count`
- `descriptor_record_count`
- `material_scope_count`
- `descriptor_instance_ambiguous`

### Readiness Contract

Readiness fields:
- `ml_blockers`: explicit reasons the numeric record should not be treated as trainable yet
- `ml_readiness_score`: heuristic 0-100 score derived from blockers
- `is_ml_ready`: boolean summary; `true` only when `ml_blockers` is empty

Relationship:
- `is_ml_ready` is authoritative for “ready vs not ready”
- `ml_readiness_score` is a prioritization aid, not a replacement for blocker checks
- `ml_blockers` must always be inspected before training

Important blockers include:
- `ambiguous_result_setting_link`
- `missing_result_setting_link`
- `descriptor_instance_ambiguous`
- `energy_basis_requires_explicit_modeling`
- `unrecognized_energy_unit`
- `missing_canonical_adsorbate`

### Provenance Contract

`provenance` retains the export safety chain:
- `review_status`
- `review_gate_status`
- `provenance_level`
- `locator_status`
- `gate_reasons`
- `safety_gate`
- `evidence_payload`

Downstream ML must not bypass this provenance chain or re-interpret blocked candidates as safe training labels.

## LM Records

`lm_records` are not numeric training labels.

They are used for:
- non-numeric DFT claims
- text-only auxiliary evidence
- LM prompting / retrieval / explanation workflows

They are not used for:
- regression/classification labels
- descriptor joins
- numeric target normalization

In practice:
- numeric pipelines should read `records`
- LM/text pipelines may read `lm_records`
- do not merge `lm_records` into numeric labels

## Downstream Usage Guidance

Recommended downstream checks:

1. Assert `metadata.schema_version == "dft_results_ml_v2"`.
2. Use `record["recommended_ml_setting_field"]` to discover the intended setting field.
3. Treat `linked_dft_setting` as the only clear training setting.
4. Require `is_ml_ready == true` before turning a numeric record into a training sample.
5. Join descriptors through `descriptor_fields`, not by reconstructing looser paper-level merges from `paper_level_dft_settings`.

## Minimal Consumer Example

```python
import json
from pathlib import Path

payload = json.loads(Path("dft_ml_dataset.json").read_text(encoding="utf-8"))
assert payload["metadata"]["schema_version"] == "dft_results_ml_v2"
assert payload["metadata"]["ml_setting_field"] == "linked_dft_setting"

train_rows = []
for record in payload["records"]:
    if not record["is_ml_ready"]:
        continue
    target = record["target"]
    train_rows.append(
        {
            "record_id": record["record_id"],
            "y": target["normalized_value"],
            "y_unit": target["normalized_unit"],
            "canonical_property_type": target["canonical_property_type"],
            "linked_dft_setting": record["linked_dft_setting"],
            "instance_key": record["sample_context"]["instance_key"],
        }
    )
```
