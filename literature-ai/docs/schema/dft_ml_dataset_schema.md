# DFT ML Dataset Schema Documentation

This document describes the JSON schema of the Machine Learning ready dataset exported by the Literature AI platform.

## 1. Top-Level Structure

The exported JSON is an object with two top-level keys:
- `metadata`: Contains dataset versioning, export parameters, and safety gate summaries.
- `records`: A list of validated DFT result records suitable for ML training.

```json
{
  "metadata": { ... },
  "records": [ { ... } ]
}
```

## 2. Metadata Schema

| Field | Type | Description |
|---|---|---|
| `dataset_version` | string | Identifier for the dataset format version. |
| `schema_version` | string | Identifier for the specific records schema version (e.g. `dft_results_ml_v1`). |
| `created_at` | string | ISO 8601 timestamp of when the export was generated. |
| `filters` | object | Dictionary of filters applied during export (e.g. `property_type`, `year_min`). |
| `safety_gate` | string | The rigorous gate rule applied (e.g. `safe_verified_with_required_evidence`). |
| `eligible_count` | integer | Number of records included in the dataset. |
| `blocked_count` | integer | Number of records blocked from the dataset due to safety constraints. |
| `blocked_reasons` | object | Frequency map of reasons why records were blocked (e.g. `missing_review`, `unsafe_locator`). |
| `total_candidates` | integer | Total number of candidates evaluated. |

## 3. Record Schema

Each object in the `records` list represents a single verified DFT data point and its associated provenance.

### 3.1. Record Identity
- `record_id` (string): The UUID of the specific `DFTResult` row.

### 3.2. Paper Information (`paper`)
- `paper_id` (string): The UUID of the source paper.
- `title` (string \| null): Paper title.
- `doi` (string \| null): Digital Object Identifier.
- `journal` (string \| null): Publication journal.
- `year` (integer \| null): Publication year.
- `authors` (list of strings \| string): Author names. Can be a list or a comma-separated string.

### 3.3. Target Property (`target`)
- `property_type` (string): The type of property calculated (e.g., `adsorption_energy`, `formation_energy`).
- `adsorbate` (string \| null): The chemical formula of the adsorbate.
- `value` (float \| null): The raw calculated numerical value extracted from the text.
- `unit` (string \| null): The raw unit of the value (e.g., `eV`, `kJ/mol`).
- `reaction_step` (string \| null): The specific reaction step this value applies to (e.g., `Li2S4 -> Li2S2`).
- `normalized_value` (float \| null): The suggested value after unit normalization (e.g., converting kJ/mol to eV).
- `normalized_unit` (string \| null): The suggested unit for the normalized value (e.g., `eV`).

### 3.4. Catalyst Information (`catalyst`)
- `catalyst_sample_id` (string): The UUID of the specific catalyst sample.
- `name` (string): Identifier or formula of the catalyst.
- `catalyst_type` (string \| null): General classification (e.g., `single_atom`, `bulk`).
- `metal_centers` (list of strings \| null): Elements acting as active sites.
- `coordination` (string \| null): Coordination environment (e.g., `Fe-N4`).
- `support` (string \| null): Substrate or support material.
- `synthesis_method` (string \| null): Brief description of how the catalyst is synthesized.
- `evidence_strength` (string \| null): Confidence level for the catalyst definition.

### 3.5. Computational Settings (`dft_settings`)
A list of computational methodologies extracted for this paper. Each setting includes:
- `dft_setting_id` (string): The UUID of the specific setting.
- `software` (string \| null): Simulation package used (e.g., `VASP`, `Gaussian`).
- `functional` (string \| null): Exchange-correlation functional (e.g., `PBE`, `B3LYP`).
- `dispersion_correction` (string \| null): Details on dispersion corrections applied (e.g., `DFT-D3`).
- `pseudopotential` (string \| null): Core electron treatment.
- `cutoff_energy_ev` (float \| null): Plane-wave cutoff energy.
- `k_points` (string \| null): K-point mesh grid.
- `convergence_settings` (object \| null): Energy or force convergence thresholds.
- `vacuum_thickness_a` (float \| null): Vacuum layer thickness in Angstroms.
- `raw_json` (object \| null): Additional unmapped setting parameters.

### 3.6. Provenance & Safety (`provenance`)
Ensures data traceability back to the exact location in the original paper.
- `source_section` (string \| null): Document section where the data was found (e.g., `Results and Discussion`).
- `source_figure` (string \| null): Specific figure or table associated with the data.
- `evidence_text` (string): The exact excerpt from the text that proves the target value.
- `confidence` (float \| null): Automated extraction confidence score.
- `review_status` (string): Current human review state (e.g., `verified`, `pending`).
- `review_gate_status` (string): Safety gate evaluation (e.g., `safe_verified`).
- `provenance_level` (string): Evidence linking strictness (e.g., `exact_page`, `in_document`).
- `locator_status` (string): Validation of the evidence text location.

Rows with `locator_status` such as `text_only`, `missing_page`, `approximate`, or `unresolved` remain blocked candidates. They can be reviewed through paper/section/evidence text, but they must not appear in ML-ready `records` until an exact PDF page locator and the required review gates exist.
