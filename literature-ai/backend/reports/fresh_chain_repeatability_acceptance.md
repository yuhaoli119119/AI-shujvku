# Fresh Chain Repeatability Acceptance

FRESH_CHAIN_REPEATABILITY_ACCEPTANCE=PASS

- Created at: `2026-06-08T17:27:42.457283+00:00`
- API base: `http://localhost:8000`
- Requested rounds: `3`
- Executed rounds: `3`
- Min real papers per round: `1`
- Target real papers per round: `3`
- Audit source: `codex_fresh_repeatability_audit`
- Total new paper count: `9`
- real_pdf_source distribution: `{'downloaded_by_pipeline': 3}`
- Below-target rounds: `[]`

## Rounds

| Round | Status | Library | Paper IDs | real_pdf_source | All Ready | Runs | Candidates | Verified | Safe Verified | Legacy Gate | Failed Paper |
| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | PASS | chain_fresh_repeatability_20260608_round01 | 6efe53f3-5c28-4b36-8ebf-3bf9426087a5, 76590237-aecc-4613-a456-a21fbd5e3d03, be9916fe-3769-4835-98c8-fb9770f6d1b4 | downloaded_by_pipeline | True | 3 | 3 | 0 | 0 | PASS |  |
| 2 | PASS | chain_fresh_repeatability_20260608_round02 | 4342baad-cda9-414f-a8c4-5eafcc97c62f, 895ebcfc-5067-434d-bb73-5a1592006059, f3105ef3-39e8-497d-9fc4-272d17100419 | downloaded_by_pipeline | True | 3 | 3 | 0 | 0 | PASS |  |
| 3 | PASS | chain_fresh_repeatability_20260608_round03 | c20187e0-c277-49d1-8f44-b8c7380813dc, 7041a455-2bdc-40ae-a65e-bb891decce9b, 10204832-d7d4-4cc8-81a9-b0a47024393b | downloaded_by_pipeline | True | 3 | 3 | 0 | 0 | PASS |  |

## Per-Paper Checks


### Round 1

| Paper ID | Title | PDF | PDF Size | Markdown | Docling | Workspace | AI Package | Local Ready | API Detail | API Codex | API Review Center | Coverage | Review Center | Candidates | Blocking Errors |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |
| 6efe53f3-5c28-4b36-8ebf-3bf9426087a5 | The rise of Single-Atom Catalysts | True | 1577029 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| 76590237-aecc-4613-a456-a21fbd5e3d03 | Transition metal-tetracyanoquinodimethane monolayers as single-atom catalysts for the electrocatalytic nitrogen reduction reaction | True | 1554879 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| be9916fe-3769-4835-98c8-fb9770f6d1b4 | Highly Active Nanoperovskite Catalysts for Oxygen Evolution Reaction: Insights into Activity and Stability of Ba0.5Sr0.5Co0.8Fe0.2O2+δ and PrBaCo2O5+δ | True | 2322654 | True | True | True | True | True | True | True | True | True | True | 1 |  |

### Round 2

| Paper ID | Title | PDF | PDF Size | Markdown | Docling | Workspace | AI Package | Local Ready | API Detail | API Codex | API Review Center | Coverage | Review Center | Candidates | Blocking Errors |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |
| 4342baad-cda9-414f-a8c4-5eafcc97c62f | Oxygen reduction reactions on pure and nitrogen-doped graphene: a first-principles modeling. | True | 180997 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| 895ebcfc-5067-434d-bb73-5a1592006059 | First principles screening of transition metal single-atom catalysts for nitrogen reduction reaction | True | 1940798 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| f3105ef3-39e8-497d-9fc4-272d17100419 | Spin State versus Potential of Zero Charge as Predictors of Density-Dependent Oxygen Reduction in M-N-C Electrocatalysts | True | 3342966 | True | True | True | True | True | True | True | True | True | True | 1 |  |

### Round 3

| Paper ID | Title | PDF | PDF Size | Markdown | Docling | Workspace | AI Package | Local Ready | API Detail | API Codex | API Review Center | Coverage | Review Center | Candidates | Blocking Errors |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |
| c20187e0-c277-49d1-8f44-b8c7380813dc | Boron-doped graphene as active electrocatalyst for oxygen reduction reaction at a fuel-cell cathode | True | 755160 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| 7041a455-2bdc-40ae-a65e-bb891decce9b | Accelerating the Discovery of g-C$_3$N$_4$-Supported Single Atom Catalysts for Hydrogen Evolution Reaction: A Combined DFT and Machine Learning Strategy | True | 6390511 | True | True | True | True | True | True | True | True | True | True | 1 |  |
| 10204832-d7d4-4cc8-81a9-b0a47024393b | Combining first-principles modeling and symbolic regression for designing efficient single-atom catalysts in Oxygen Evolution Reaction on Mo$_2$CO$_2$ MXenes | True | 1077408 | True | True | True | True | True | True | True | True | True | True | 1 |  |
