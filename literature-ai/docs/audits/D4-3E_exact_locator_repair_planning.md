# D4-3E Exact Locator Repair Planning / Read-only Locator Feasibility Gate

Date: 2026-05-27

Scope: read-only feasibility audit for recovering exact locators for the five pending pilot review rows on paper `3978dc79f94f4457863fd68449ae293d`. No DB row, locator, review, extraction output, materialized fact, registry entry, or artifact was written or cleaned up.

## 1. Baseline / Sync

Required preflight commands:

- `git status --short`: clean
- `git log -1 --oneline`: `4c43507 feat d4 controlled prepare entry ux`
- `git rev-parse HEAD`: `4c43507fca86af4c3ae0b0eb35e38f309da9f246`
- `git branch -vv`: `* master 4c43507 [origin/master] feat d4 controlled prepare entry ux`
- `git fetch origin`: succeeded
- `git ls-remote origin refs/heads/master`: `4c43507fca86af4c3ae0b0eb35e38f309da9f246 refs/heads/master`

Conclusion: local `HEAD`, local `origin/master`, and remote `refs/heads/master` were identical at `4c43507fca86af4c3ae0b0eb35e38f309da9f246`.

## 2. Code Paths Read

Audit files read:

- `D4-3A_controlled_single_paper_review_pilot.md`
- `D4-3B_single_paper_active_pending_review_smoke.md`
- `D4-3C_human_workbench_pending_queue_ux.md`
- `D4-3D.1_prepare_reviews_idempotency_guard.md`
- `D4-3D.2_controlled_prepare_entry_ux.md`
- `D4-2E_red_locator_shape_triage.md`

Relevant code read:

- `backend/app/utils/active_database.py`
- `backend/app/utils/project_paths.py`
- `backend/app/utils/artifact_paths.py`
- `backend/app/services/evidence_locator_service.py`
- `backend/app/utils/locator_degradation.py`
- `backend/app/services/extraction_review_service.py`
- `backend/app/utils/review_safety.py`

Key implementation facts:

- active DB discovery prefers the canonical registry path under `literature-ai/data/library_registry.json`, then scans candidate SQLite DBs.
- persisted artifacts are resolved through storage/category-aware path helpers.
- locator safety requires a valid positive page for `exact_page`; missing page degrades to text-only/missing-page and disables jump/highlight.
- `prepare_pending_reviews()` only creates/reuses `pending` rows and does not mark verified.
- export/writing safety requires safe verified review plus required evidence and exact PDF-page locator.

## 3. Active DB Read-only Confirmation

Read mode: SQLite URI `mode=ro`.

- canonical registry path: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\library_registry.json`
- registry active library: present, but root path contains historical mojibake in JSON
- effective active DB path used for read-only SQL: `D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\data\libraries\default\database.sqlite`
- active DB exists: yes
- `papers_total`: 15
- pilot paper exists: yes
- pilot title: `锂硫电池非均相电催化剂`
- pilot PDF: yes
- pilot markdown: yes
- pilot docling JSON: yes
- pilot TEI/XML: yes
- `extraction_field_reviews` for pilot: 5
- pending rows: 5
- verified rows: 0
- safe verified rows: 0
- DFT result rows: 0
- evidence locators: 0
- evidence spans: 11, all currently `page=NULL`
- export eligible count: 0
- writing eligible count: 0

Pilot paper artifact fields:

- PDF: `storage/pdf/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.pdf`
- TEI: `storage/tei/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.tei.xml`
- Docling JSON: `storage/docling_json/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.docling.json`
- Markdown: `storage/markdown/cf79612a-b912-41f2-a759-2ba4a41661c4_2022-锂硫电池非均相电催化剂.md`

## 4. Artifact Inventory

Files observed:

- PDF: exists, 2,870,462 bytes, PyMuPDF text layer readable, 15 pages.
- Markdown: exists, 64,834 bytes.
- Docling JSON: exists, 496,741 bytes, 586 text-like items found by recursive read-only inspection.
- TEI/XML: exists, 205,538 bytes.

Docling artifacts contain page-level provenance with bbox values for many text, figure, and table items. The DB did not persist these locators into `evidence_locators`; `paper_figures.prov` and `paper_tables.prov` also contain Docling page/bbox, while `paper_sections.page_start/page_end` and `evidence_spans.page` are null.

## 5. Five Pending Review Rows Inventory

All five rows:

- `reviewer_status=pending`
- serialized safe `verified=false`
- `target_resolution_status=active`
- `reviewer_note=prepared_from_extraction`
- currently no locator row
- current locator state in frontend/API is `missing_page / unsafe_locator`
- currently export/writing safe: no

| Review ID | Target type | Field | Value | Evidence text | Evidence reference | Locator status | Page | Bbox raw | Safety |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896` | `catalyst_samples` | `name` | `Fe-Co-V` | `HAADF-STEM` | yes, via target evidence spans | `missing_page` | null | null | blocked, unsafe locator |
| `09f83676-8f13-4e82-a576-ab359b264933` | `catalyst_samples` | `catalyst_type` | `single_atom` | `HAADF-STEM` | yes, via target evidence spans | `missing_page` | null | null | blocked, unsafe locator |
| `280f2d9e-3ebb-4107-9702-f6ea6d645465` | `catalyst_samples` | `metal_centers` | `["Fe","Co","V"]` | `HAADF-STEM` | yes, via target evidence spans | `missing_page` | null | null | blocked, unsafe locator |
| `4ba0e490-5934-439c-8136-33a8ddf4e201` | `dft_settings` | `convergence_settings` | reproducibility skeleton, score 0/high risk | extracted-empty settings dict | no useful DFT evidence source | `missing_page` | null | null | blocked, unsafe locator |
| `56f72584-45b3-465b-9a40-97ec60a2fabf` | `electrochemical_performance` | `rate` | `0.2C` | Figure 5 cycling-performance caption fragment | yes, via target evidence spans | `missing_page` | null | null | blocked, unsafe locator |

## 6. Evidence Text to Artifact Matching Result

Matching methods used:

- exact string search over markdown/TEI/Docling JSON
- whitespace and punctuation normalization
- compact alphanumeric/CJK normalization for Chinese/English mixed strings
- PDF text-layer `search_for()` using PyMuPDF, no OCR and no new dependency

### Catalyst rows

Current pending-row evidence text is only `HAADF-STEM`. That exact string appears multiple times:

- PDF page 3, bbox around `393.62,256.05,458.08,266.06`
- PDF page 7, bbox around `389.71,301.80,454.16,311.81`
- PDF page 8, three occurrences
- PDF page 9, two occurrences
- Docling JSON also has multiple `HAADF-STEM` hits.

Therefore `HAADF-STEM` alone is ambiguous and should not be used by itself for repair.

Better target-specific evidence exists in existing artifacts:

- target span `single-atom catalyst, SAC` matched markdown and Docling `/texts/79`, page 7, bbox `{l:53.858,t:477.052,r:287.155,b:359.672}`, confidence source span 0.65.
- target span about `铁(Fe)`, `钴(Co)`, `钒(V)` matched markdown and Docling `/texts/80`, page 7, bbox `{l:53.859,t:356.594,r:287.167,b:70.085}`, confidence source span 0.60.
- PDF text layer confirms page 7 contains the single-atom catalyst and Fe/Co/V/TM-N4-C discussion.
- PDF text search finds `single-atom catalyst` on page 7 with bbox around `198.50,362.75,284.57,372.75`.
- PDF text search finds `TM = Co, Fe, V` on page 7 with bbox around `187.50,469.45,258.94,479.45`.

Blocker: the materialized `name=Fe-Co-V` does not appear as that exact string in artifacts. It is an extraction-normalized aggregate of Fe/Co/V metal-center mentions, not a literal paper term.

### DFT settings row

Current evidence text is an extracted empty-settings dictionary, not a paper quote.

Findings:

- exact extracted-empty dictionary: no match in markdown, TEI, or Docling text.
- `convergence criteria`: no match.
- `k-points`: no match.
- generic `DFT` and `functional` occur many times but are not evidence for the extracted reproducibility skeleton.

This row cannot support exact locator repair from current evidence. It should remain RED and should not be locator-repaired without a better extraction source.

### Electrochemical rate row

Current evidence text is a stitched caption fragment. Exact full-row text does not match because it combines two caption fragments, but strong subspans do match:

- `S/TiN-VN@CNFs` matched markdown, TEI, Docling `/texts/74`.
- Docling `/texts/74`: page 6, bbox `{l:53.858,t:125.995,r:541.43,b:71.087}`, text includes Figure 5 cycling performance and `0.2 C`.
- PDF text layer finds `S/TiN-VN@CNFs||Li` on page 6 and page 7.
- PDF text layer finds `Cycling performances` on page 6 with bbox around `53.86,705.28,124.67,713.28`.
- PDF text layer finds `0.2 C` on page 6 with bboxes around `250.08,645.30,267.43,653.30` and `471.06,705.28,488.42,713.28`.

Blocker: the DB evidence text combines a Figure 5 caption fragment with another NC/MoS3 caption/reference fragment, so a repair should pick the Figure 5 source only, not the stitched full text.

## 7. Locator Feasibility Table

| Review ID | Field | Classification | Matched artifact path | Matched page | Match method | Confidence | Repair feasibility | Blockers |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `e2c75b7f-2d9c-41ff-a6e1-e95e5d491896` | `name` | YELLOW | docling JSON `/texts/80`; PDF text layer | 7 | compact normalized Fe/Co/V span match; PDF text search | 0.60 source confidence, locator candidate medium | Possible only as target-level page/bbox, not exact `Fe-Co-V` literal | current evidence `HAADF-STEM` ambiguous; `Fe-Co-V` not literal in paper |
| `09f83676-8f13-4e82-a576-ab359b264933` | `catalyst_type` | YELLOW | docling JSON `/texts/79`; PDF text layer | 7 | normalized `single-atom catalyst/SAC` match; PDF text search | 0.65 source confidence, locator candidate medium-high | Good candidate for controlled repair if evidence text is replaced/anchored to SAC span | current evidence `HAADF-STEM` ambiguous and should not be sole locator source |
| `280f2d9e-3ebb-4107-9702-f6ea6d645465` | `metal_centers` | YELLOW | docling JSON `/texts/80`; PDF text layer | 7 | compact normalized Fe/Co/V span match; PDF text search | 0.60 source confidence, locator candidate medium | Good candidate for page/bbox repair using Fe/Co/V sentence | current evidence `HAADF-STEM` ambiguous |
| `4ba0e490-5934-439c-8136-33a8ddf4e201` | `convergence_settings` | RED | none | none | exact/normalized text search failed | 0.0 | Not repairable from current artifacts/evidence | evidence is an extracted empty-settings dict, not source text; no DFT settings source |
| `56f72584-45b3-465b-9a40-97ec60a2fabf` | `rate` | YELLOW | docling JSON `/texts/74`; PDF text layer | 6 | subspan match for Figure 5 caption, `S/TiN-VN@CNFs`, `0.2 C`, `Cycling performances` | 0.68 source confidence, locator candidate medium-high | Good candidate for controlled repair if split from stitched unrelated caption tail | current full evidence text is stitched across sources; exact full-text match fails |

No row is GREEN under the strict D4-3E definition because none has a current persisted exact locator, and several current review evidence strings are ambiguous or synthetic. Four rows have viable page/bbox candidates in Docling/PDF text layer if D4-3F is allowed to use target-specific evidence spans/subspans instead of blindly matching the current pending-row `evidence_text`.

## 8. Recommendation for D4-3F

Recommendation: do not run a direct locator repair that simply writes locators from current pending-row `evidence_text`.

Better next step: D4-3F should implement a controlled locator recovery helper with explicit review output before DB write. The helper should:

- read current pending rows and target evidence spans
- prefer target-specific evidence spans over low-information pending-row evidence text
- use Docling `prov.page_no` and `bbox` as the primary locator candidate source
- verify candidate with PDF text-layer search when possible
- reject ambiguous short evidence such as `HAADF-STEM` unless disambiguated by target span or field/value context
- reject synthetic/materialized values that do not literally appear in source text
- produce a proposed repair manifest for human approval before any DB write

Suggested D4-3F scope:

- allow controlled repair proposals for the three catalyst rows and the electrochemical `rate` row
- keep `dft_settings.convergence_settings` RED and unrepaired
- do not mark verified
- do not unlock export/writing

## 9. Verification

Required docs/read-only check:

- `git diff --check`: to be run after this manifest is staged/reviewed.

No frontend Playwright run is required because no frontend files changed.

No backend pytest run is required because no backend code changed.

No auxiliary script file was created or committed. Read-only SQL/artifact scans were executed via inline PowerShell/Python stdin with `PYTHONDONTWRITEBYTECODE=1`; no helper file remains.

## 10. Safety Confirmation

- active DB write: no
- verified review write: no
- `mark_verified`: no
- `save_reviews`: no
- locator write: no
- extraction/reprocessing apply: no
- materialize: no
- migration apply: no
- export/writing unlock: no
- artifact cleanup: no
- DB copy/move/delete: no
- registry write: no

## 11. Remaining Risks

- Current DB `evidence_spans.page` and `paper_sections.page_start/page_end` remain null.
- Existing `evidence_locators` count for the pilot paper remains 0.
- Docling/PDF page numbers are promising but not yet persisted or human-approved.
- `HAADF-STEM` is too ambiguous to repair without target-specific disambiguation.
- `Fe-Co-V` appears to be a normalized aggregate, not a literal source phrase.
- The DFT settings row is not repairable from current evidence.
- The rate row evidence is stitched from multiple source fragments and needs splitting before any locator repair.
- Export/writing remain correctly blocked until exact locators and explicit safe human verification exist.
