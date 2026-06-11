# Batch Acceptance 20260611

BATCH_ACCEPTANCE_20260611=PARTIAL_PASS

- Library: `batch_acceptance_20260611`
- Baseline commit: `600663df709f9476d0ca25acee924065569a0832`
- Runtime: Docker backend/PostgreSQL, API `http://localhost:8000`
- Scope: 10 newly ingested real papers, PDF intake, sample/DFT extraction, DFT-sample binding, dual-AI settlement, reject, ML_Ready gate, DFT dataset export.
- Selection coverage: at least 7 table-style DFT papers, at least 7 multi/comparative-material papers, at least 3 figure/text mixed papers, and 1 complex 2026 M-N-C PDF.
- Small blocking fix applied: `/api/papers/discovery/download` now converts metadata-miss `ValueError` to HTTP 404 instead of uncaught 500.
- DFT dataset export: `GET /api/papers/export/dft-dataset?library_name=batch_acceptance_20260611` returned HTTP 200, `eligible_count=3`, `blocked_count=86`, `total_candidates=89`.

## Acceptance Table

| # | DOI / identifier | Title | PDF downloadable | Parsed | Samples | DFT | New sample | Bindings | Reject | ML_Ready | needs_review | Blocked reasons |
| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 10.1039/d0ma00348d | Transition metal-tetracyanoquinodimethane monolayers as single-atom catalysts for the electrocatalytic nitrogen reduction reaction | yes | yes | 1 | 6 | 0 | 0 | 1 | 0 | 5 | missing_material_identity=5; missing_review=5; unsafe_locator=5 |
| 2 | 10.48550/arxiv.2211.01624 | Accelerating the Discovery of g-C3N4-Supported Single Atom Catalysts for Hydrogen Evolution Reaction | yes | yes | 1 | 4 | 0 | 0 | 0 | 0 | 4 | missing_material_identity=4; missing_review=4; unsafe_locator=4 |
| 3 | 10.48550/arxiv.2305.19551 | Combining first-principles modeling and symbolic regression for designing efficient single-atom catalysts in OER on Mo2CO2 MXenes | yes | yes | 1 | 1 | 0 | 0 | 0 | 0 | 1 | missing_material_identity=1; missing_review=1; unsafe_locator=1 |
| 4 | 10.1039/c1nr11307k | Oxygen reduction reactions on pure and nitrogen-doped graphene: a first-principles modeling | no, metadata only | no | 0 | 0 | 0 | 0 | 0 | 0 | 0 | PDF source unavailable from discovery download |
| 5 | 10.48550/arxiv.2604.17427 | Spin State versus Potential of Zero Charge as Predictors of Density-Dependent Oxygen Reduction in M-N-C Electrocatalysts | yes | yes | 1 | 3 | 0 | 0 | 0 | 0 | 3 | missing_material_identity=3; missing_review=3; unsafe_locator=3 |
| 6 | 10.1021/acs.jpcc.3c02224 | Oxygen Reduction Reaction on Single-Atom Catalysts From DFT Calculations Combined with an Implicit Solvation Model | yes | yes | 2 | 18 | 1 | 3 | 0 | 3 | 15 | missing_material_identity=15; missing_review=15 |
| 7 | 10.1016/j.susc.2022.122144 | Comparative density functional theory study for predicting oxygen reduction activity of single-atom catalyst | yes | yes | 1 | 42 | 0 | 0 | 0 | 0 | 42 | missing_material_identity=42; missing_review=42; unsafe_locator=1 |
| 8 | 10.1016/j.apsusc.2022.155916 | High-Throughput Screening of Transition Metal Single-Atom Catalysts for Nitrogen Reduction Reaction | yes | yes | 1 | 8 | 0 | 0 | 0 | 0 | 8 | missing_material_identity=8; missing_review=8; unsafe_locator=8 |
| 9 | 10.1002/qua.26956 | Accelerating the theoretical study of Li-polysulphide adsorption on single-atom catalysts via machine learning approaches | yes | yes | 1 | 1 | 0 | 0 | 0 | 0 | 1 | missing_material_identity=1; missing_review=1; unsafe_locator=1 |
| 10 | 10.1016/j.jcat.2014.07.024 | Boron-Doped Graphene As Active Electrocatalyst For Oxygen Reduction Reaction At A Fuel-Cell Cathode | yes | yes | 1 | 6 | 0 | 0 | 1 | 0 | 5 | missing_material_identity=5; missing_review=5; unsafe_locator=5 |

## Totals

| Metric | Count |
| --- | ---: |
| Papers in test library | 10 |
| Complete PDF + Docling parse | 9 |
| Metadata-only source blocker | 1 |
| Samples | 10 |
| DFT candidates | 89 |
| DFT-sample bindings | 3 |
| Rejected noise candidates | 2 |
| ML_Ready | 3 |
| needs_review / blocked candidates | 84 |

## ML_Ready Proof

All three ML_Ready rows are from DOI `10.1021/acs.jpcc.3c02224`, Table 1, PDF page 13. A precise `Co-N4-C` sample was created by dual-AI consensus to avoid binding to the broad extracted `Fe-N-C / graphene` sample.

| DFT result id | catalyst_sample_id | Property | Value | Evidence anchor | Gate | Dual AI evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 0d15f57e-64d3-4045-a2d2-b87809cc5f01 | 272122ff-f431-432b-bb8c-c73802264ba9 | limiting_potential | 0.80 V | page 13, Table 1, Co-N4-C / constant-mu e | eligible, exact_pdf_page/exact_page | primary + secondary materialized value and catalyst_sample_id opinions |
| 3846812c-bfda-4e58-8ae9-e079dee1a3ed | 272122ff-f431-432b-bb8c-c73802264ba9 | limiting_potential | 0.72 V | page 13, Table 1, Co-N4-C / constant-N e ESM-RISM | eligible, exact_pdf_page/exact_page | primary + secondary materialized value and catalyst_sample_id opinions |
| ef6ed284-680d-4646-8688-5a6f5e3588e9 | 272122ff-f431-432b-bb8c-c73802264ba9 | limiting_potential | 0.93 V | page 13, Table 1, Co-N4-C / constant-N e vacuum | eligible, exact_pdf_page/exact_page | primary + secondary materialized value and catalyst_sample_id opinions |

## Failure Classes

- Metadata/PDF source unavailable: 1 paper, DOI `10.1039/c1nr11307k`, imported as metadata only.
- Metadata provider miss originally surfaced as 500 for DOI-only requests: fixed by returning 404; arXiv identifiers completed the affected PDFs.
- Parser numeric noise: 2 rows rejected through dual-AI consensus.
- Multi-material ambiguity / missing material identity: dominant blocker, 86 blocked export candidates before excluding rejected rows.
- Unsafe or missing exact locator: 30 blocked export candidates still need page/table-aware repair before export.

## Current Product Blocker

The single remaining product blocker is material-specific table binding: extraction can detect many DFT values from comparative tables, but it often does not preserve the exact material column/row as `catalyst_sample_id` plus exact page/table locator. The safety gate correctly blocks those rows instead of silently exporting ambiguous data.

## Verdict

- At least 6/10 papers reached a judgment state: yes, 10/10 were judged; 9/10 completed PDF parse and 1/10 is a source blocker.
- At least 3 ML_Ready rows: yes, 3 rows.
- No evidence-free or ambiguous multi-material rows silently entered ML_Ready: yes.
- Ready for first round human real testing: yes, with the known table material-binding blocker visible in review queues.

