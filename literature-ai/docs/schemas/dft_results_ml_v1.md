# dft_results_ml_v1 Schema

`dft_results_ml_v1` is the read-only ML export schema returned by:

```text
GET /api/papers/export/dft-dataset
```

The endpoint keeps the existing safety gate strict: only DFT rows with `safe_verified` human review, required evidence text/reference, and an exact PDF locator are included in `records`. Text-only rows with paper/section/evidence text but no PDF page remain review candidates only. Blocked candidates are summarized in `metadata.blocked_reasons`; they are not exported as training facts.

## Top-Level Shape

```json
{
  "metadata": {
    "dataset_version": "dft-ml-dataset-v0.1",
    "schema_version": "dft_results_ml_v1",
    "created_at": "2026-05-30T00:00:00+00:00",
    "filters": {
      "property_type": "adsorption_energy",
      "adsorbate": "Li2S4",
      "year_min": 2020,
      "year_max": 2026
    },
    "safety_gate": "safe_verified_with_required_evidence",
    "eligible_count": 1,
    "blocked_count": 2,
    "blocked_reasons": {
      "missing_review": 1,
      "unsafe_locator": 1
    },
    "total_candidates": 3
  },
  "records": []
}
```

## Record Fields

Each item in `records` has these blocks:

| Field | Type | Notes |
| --- | --- | --- |
| `record_id` | string | Source `dft_results.id`. |
| `paper` | object | Source paper identity: `paper_id`, `title`, `doi`, `journal`, `year`, `authors`. |
| `target` | object | ML target: `property_type`, `adsorbate`, `value`, `unit`, `reaction_step`. |
| `catalyst` | object or null | Primary linked catalyst sample when available. |
| `catalyst_candidates` | array | Other catalyst samples from the same paper for downstream disambiguation. |
| `dft_settings` | array | DFT settings from the same paper, including raw settings JSON. |
| `provenance` | object | Evidence chain and safety gate state for audit. |

`provenance.review_gate_status` must be `safe_verified` for exported records. `provenance.locator_status` must be `exact_page`.

## Blocked Reasons

`metadata.blocked_reasons` may include:

| Reason | Meaning |
| --- | --- |
| `missing_review` | No human review exists for the DFT row. |
| `unsafe_review` | Review exists but is not safe verified or target resolution is unsafe. |
| `missing_evidence` | No required evidence reference/span/locator exists. |
| `missing_evidence_text` | The DFT row has no evidence text. |
| `unsafe_locator` | Evidence exists, but locator is not an exact PDF page. |

## Python Reading Example

```python
import json
from pathlib import Path

path = Path("dft_ml_dataset.json")
payload = json.loads(path.read_text(encoding="utf-8"))

metadata = payload["metadata"]
assert metadata["schema_version"] == "dft_results_ml_v1"
assert metadata["safety_gate"] == "safe_verified_with_required_evidence"

rows = []
for record in payload["records"]:
    target = record["target"]
    paper = record["paper"]
    provenance = record["provenance"]
    rows.append(
        {
            "record_id": record["record_id"],
            "paper_id": paper["paper_id"],
            "doi": paper.get("doi"),
            "property_type": target["property_type"],
            "adsorbate": target["adsorbate"],
            "value": target["value"],
            "unit": target["unit"],
            "review_gate_status": provenance["review_gate_status"],
            "locator_status": provenance["locator_status"],
        }
    )

print(f"loaded {len(rows)} safe ML records")
print("blocked summary:", metadata["blocked_reasons"])
```

For pandas:

```python
import json
import pandas as pd

with open("dft_ml_dataset.json", "r", encoding="utf-8") as f:
    payload = json.load(f)

df = pd.json_normalize(payload["records"], sep=".")
print(df[["record_id", "target.property_type", "target.adsorbate", "target.value", "target.unit"]])
```

## Unit Normalization Guidance

Do not overwrite raw exported values. If a downstream pipeline normalizes units, keep derived columns separate, for example `target.value_ev` or `target.value_si`, and retain `target.value` plus `target.unit` for traceability.
