# D6 Artifact Reliability Active Snapshot

**Run time:** 2026-06-10 03:55:20 +08:00  
**Mode:** Read-only active-library snapshot  
**HEAD:** `daab767 feat: add artifact reliability audit report`

This snapshot records the first active-library run of the artifact and locator reliability report. It is a measurement artifact only: no database rows were changed, no figures were recropped, no locators were repaired, no parser logic was changed, and no candidate was verified, approved, or merged.

## Runtime State

- Docker services: backend, PostgreSQL, Redis, worker, MinIO, and GROBID were running.
- Health endpoint: `GET http://localhost:8000/api/health` returned `status=ok`.
- Database kind: PostgreSQL.
- Database: `literature_ai`.
- Active library reported by health: `石墨炔`.
- Storage root in container: `/data/storage`.
- Storage root used for local read-only report: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\storage`.
- DB paper count: 30.
- Papers sampled by report: 30.

The running backend container did not yet include the new endpoint and returned `404` for `/api/workbench/artifact-reliability`. The snapshot therefore used the current local code against the same PostgreSQL database through `localhost:5432`, then confirmed the new read-only API shape with local `TestClient`:

- `GET /api/workbench/artifact-reliability?limit=500`: `200`, sampled `30`.
- `GET /api/workbench/papers/260e6e27-255c-4d87-a4ad-0e9a1ec11114/artifact-reliability`: `200`, issue count `54`.

## Overall Counts

### Figure Issues

| Issue | Count |
| --- | ---: |
| `missing_full_page_snapshot` | 197 |
| `small_crop` | 12 |

### Table Issues

| Issue | Count |
| --- | ---: |
| `missing_bbox` | 1 |

### Locator Issues

| Issue | Count |
| --- | ---: |
| `text_only_locator` | 195 |
| `missing_bbox` | 82 |

## Worst Papers

| Rank | Issue Count | Figures | Tables | Locators | Title |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 54 | 18 | 5 | 32 | Theoretical Investigation of Diameter Effects and Edge Configuration on the Optical Properties of Graphdiyne Nanotubes in the Presence of Electric Field |
| 2 | 49 | 11 | 2 | 37 | Detection of odor quality and ripening stage of Mangifera indica L. by graphdiyne nanosheet - a DFT outlook |
| 3 | 33 | 4 | 0 | 29 | Adsorption, diffusion and aggregation of Ir atoms on graphdiyne: a first-principles investigation |
| 4 | 29 | 16 | 4 | 13 | Boron-graphdiyne: a superstretchable semiconductor with low thermal conductivity and ultrahigh capacity for Li, Na and Ca ion storage |
| 5 | 27 | 5 | 0 | 22 | Surfactant-free interfacial growth of graphdiyne hollow microspheres and the mechanistic origin of their SERS activity |

## Representative Examples

### `text_only_locator`

- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `dft_settings`, page: none, status: `text_only`  
  Evidence: "The systems were simulated by a repeated slab model with a vacuum layer of 20 A inserted in the perpendicular directions."  
  Reason: page missing from parser output.
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `catalyst_samples`, page: none, status: `text_only`  
  Evidence: "The remarkable success in preparing graphene (GP)..."  
  Reason: page missing from parser output.
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `dft_results`, page: none, status: `text_only`  
  Evidence: "performed for the most stable configurations..."  
  Reason: page missing from parser output.

### `missing_bbox`

- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `dft_results`, page: 4, status: `exact_page`  
  Evidence: "E g (eV): 0.44; row: PBE | 0.44 | 0.46 | 0.50 | 0.40 | 0.48"  
  Reason: bbox unavailable.
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `dft_results`, page: 4, status: `exact_page`  
  Evidence: "E g (eV): 0.46; row: PBE | 0.44 | 0.46 | 0.50 | 0.40 | 0.48"  
  Reason: bbox unavailable.
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Target: `dft_results`, page: 4, status: `exact_page`  
  Evidence: "E g (eV): 0.50; row: PBE | 0.44 | 0.46 | 0.50 | 0.40 | 0.48"  
  Reason: bbox unavailable.

### `missing_full_page_snapshot`

- Paper: Graphdiyne as a promising material for detecting amino acids  
  Figure page: 2, status: `needs_review`  
  Caption: "Figure 1. (a) The (2 x 2)/(10 x 7) hexagonal supercell of the GD/GP layer and their Brillouin zone."
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Figure page: 5, status: `needs_review`  
  Caption: "Figure 2. (a) The energy bands and DOS of GD and GD-Gly with (2 x 2) GD supercell at the PBE/HSE06 level."
- Paper: Graphdiyne as a promising material for detecting amino acids  
  Figure page: 5, status: `needs_review`  
  Caption: "Figure 3. (a) The sketch of the absorption spectrum measurement for GD-AA..."

### `small_crop`

- Paper: Two-Dimensional Second-Order Topological Insulator in Graphdiyne  
  Figure page: 2, status: `needs_review`  
  Caption: "FIG. 1. (a) Crystal structure of GDY. (b) shows the Brillouin zone."
- Paper: Two-Dimensional Second-Order Topological Insulator in Graphdiyne  
  Figure page: 3, status: `needs_review`  
  Caption: "FIG. 3. (a) Energy spectrum of the hexagonal-shaped GDY nanodisk shown in (b)."
- Paper: Two-Dimensional Second-Order Topological Insulator in Graphdiyne  
  Figure page: 4, status: `needs_review`  
  Caption: "FIG. 4. (a) Schematic figure showing that two edges related by My must have opposite Dirac mass..."

## Interpretation

The issue distribution is dominated by locator reliability:

- `text_only_locator` appears 195 times and blocks direct PDF jumping.
- `missing_bbox` appears 82 times on locators that often still have exact pages, meaning page-level review is possible but precise highlight is not.
- Figure issues are also broad: `missing_full_page_snapshot` appears 197 times. This is useful for figure audit UI, but less immediately blocking than text-only locators because many figures still have captions and pages.
- Table issues are minimal in this snapshot, with only one `missing_bbox` case.

## Recommended Next Step

Prioritize locator reliability visibility first:

1. Add a compact locator reliability warning to the DFT queue or Review center, using the existing exact-page/text-only/missing-page semantics.
2. Show counts for `text_only_locator`, `missing_page`, and `missing_bbox` near the existing DFT evidence/export gate so reviewers understand whether a candidate can be jumped to the PDF.
3. Keep actions read-only at first: no locator repair, no inferred page, no bbox fabrication, no approval or verification.

Figure reliability should be the second UI step:

1. Add figure reliability warnings in the literature library figure detail or review center summary.
2. Prioritize `missing_full_page_snapshot` and `small_crop` display.
3. Defer table UI until more table issues appear in a larger sample.

## Boundary

This snapshot is read-only. It does not repair evidence, regenerate artifacts, recrop figures, trust OCR or bbox automatically, change parser behavior, update `PaperFigure`, `PaperTable`, `EvidenceLocator`, or `Paper` rows, or alter any verified/approve/merge workflow.
