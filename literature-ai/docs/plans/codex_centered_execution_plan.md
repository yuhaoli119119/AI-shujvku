# Codex-Centered Execution Plan

## Final Goal

Turn `literature-ai` into a local literature workbench for Codex. The app should not pretend to make final scholarly judgments. It should collect papers, parse PDFs, locate evidence, store structured candidates, support DFT database export, and retrieve writing evidence. Codex then performs final reading, screening, verification, synthesis, review writing, and citation selection.

## Success Criteria

- Codex can read a compact single-paper bundle containing metadata, abstract, sections, figures, tables, DFT candidates, mechanism candidates, writing cards, evidence locators, notes, references, and external-analysis candidates.
- Every automatic output is explicitly marked candidate / unverified / needs_review.
- PDF artifacts support reading: text, pages, captions, table content, image paths, and figure crops are available. Unreliable images are downgraded instead of silently trusted.
- DFT rows are traceable candidates only. ML-ready export requires evidence and manual/Codex review gates.
- The app can run five downloadable graphite/graphene defect papers end to end: download/import, parse, build Codex context, inspect DFT/mechanism candidates, and report quality.

## Implemented

- HTTP API: `GET /api/papers/{paper_id}/codex-context`.
- HTTP API: `GET /api/papers/{paper_id}/codex-item/{item_type}/{item_id}` for one figure, table, section, DFT candidate, mechanism claim, writing card, or other structured item.
- HTTP API: `POST /api/papers/{paper_id}/dft-results/{result_id}/verify` for explicitly reviewed DFT candidates.
- HTTP API: `POST /api/papers/{paper_id}/dft-results/{result_id}/reject` for explicitly rejected noisy DFT candidates.
- HTTP API: `POST /api/papers/{paper_id}/dft-results/{result_id}/corrections` for pending DFT field correction proposals that do not auto-apply.
- HTTP API: `GET /api/papers/export/dft-review-queue` for a Codex-ready queue of DFT candidates that need review before ML export.
- MCP tool: `get_codex_context`.
- MCP tool: `get_codex_item`.
- MCP tool: `get_dft_review_queue`.
- MCP tool: `verify_dft_result`.
- MCP tool: `reject_dft_result`.
- MCP tool: `propose_dft_result_correction`.
- Library UI actions: `Copy Codex paper bundle` and per-item `Copy this item for Codex`.
- DFT UI action: row-level `Mark verified` after PDF/evidence review.
- DFT database quality panel now uses the review queue and can mark a row verified or reject a noisy candidate.
- DFT database quality panel can submit pending correction proposals for one candidate field while preserving curator approval as the application gate.
- JSON and Markdown bundles with reliability policy, warnings, locator summary, candidate status, and recommended next actions.
- Figure entries now include `prov`, bbox, image dimensions, local path, and `image_review` so Codex can tell whether a crop is a usable figure, a subfigure, or needs review.
- DFT context now includes `dft_export_readiness`, with per-row safety results from the review/evidence/locator gate.
- The DFT tab shows export safety counts and row-level blockers before any ML export.
- DFT review queue rows include `sanity_flags` so citation-like adsorbates, unexpected units, and suspicious magnitudes are not shown as directly verifiable.
- DFT extraction now drops reference-table artifacts and non-numeric electronic-structure claims from the numeric DFT result table.
- Isolated real-data regression can force a configured SQLite/storage pair with `LITAI_FORCE_CONFIGURED_DATABASE=true`.
- Repeatable real-PDF QA script: `backend/tools/codex_graphite_defect_e2e.py`.

## Real PDF E2E Run

Run:

```bash
cd backend
python tools/codex_graphite_defect_e2e.py
```

Latest local report:

`backend/tests/data/codex_graphite_defects_e2e/reports/20260604T011000Z.json`

That directory is ignored by git, so PDFs, SQLite databases, parser artifacts, and reports are not versioned.

| arXiv | Topic | Status | Parsed counts |
| --- | --- | --- | --- |
| [1710.10084](https://arxiv.org/abs/1710.10084) | Graphene single vacancy adsorption DFT | completed | 53 sections, 16 figures, 6 tables, 1 DFT setting, 1 DFT result |
| [2308.05425](https://arxiv.org/abs/2308.05425) | Stone-Wales defect reactivity DFT | completed | 48 sections, 15 figures, 8 tables, 1 DFT setting, 15 DFT results, 3 mechanism claims |
| [1405.1928](https://arxiv.org/abs/1405.1928) | Point defects in twisted bilayer graphene | completed | 37 sections, 15 figures, 5 tables, 1 DFT setting, 9 DFT results |
| [1112.5598](https://arxiv.org/abs/1112.5598) | Divacancies in irradiated graphene | completed | 10 sections, 5 figures, 1 DFT setting |
| [1207.3194](https://arxiv.org/abs/1207.3194) | DFT/DFTB calculations of graphene defects | completed | 13 sections, 4 figures, 2 tables, 1 DFT setting, 6 DFT results |

## Findings

- All five PDFs downloaded, ingested, parsed, and produced Codex context bundles.
- The latest run completed in 261.03 seconds and produced 161 sections, 55 figures, 21 tables, 5 DFT settings, 31 DFT result candidates, 3 mechanism claims, 5 writing cards, and 44 evidence locators.
- DFT output is not blank anymore. For example, 1207.3194 produced vacancy migration and Stone-Wales barrier candidates.
- All 31 DFT candidates had export-readiness entries and were blocked from ML export before review because they still needed review (`missing_review`). This is the intended safety behavior.
- The row-level verification path was tested on a copied five-paper SQLite run. Before verification, ML export had 0 eligible and 31 blocked DFT rows. After explicitly verifying one evidence-backed real DFT row from 1710.10084, ML export had 1 eligible and 30 blocked rows.
- The DFT review queue was then tested on a fresh copied five-paper SQLite run. It returned all 31 candidates, flagged 17 suspicious candidates, and left 14 candidates directly eligible for PDF/evidence review. One real `reaction_barrier = 0.87 eV` row was verified through the queue, moving ML export from 0 eligible / 31 blocked to 1 eligible / 30 blocked.
- The real queue test caught a bad candidate (`adsorbate="[22]"`, `value=436.0`, `unit="e"`) and now flags it with `adsorbate_looks_like_reference` and `unexpected_potential_unit:e` instead of offering direct verification.
- A follow-up copied five-paper run rejected that same bad candidate and verified one real `reaction_barrier = 0.87 eV` row. The active review queue dropped from 31 rows to 29 rows, the rejected queue contained the bad candidate, and ML export reported 1 eligible / 30 blocked with blocked reasons `missing_review: 29` and `unsafe_review: 1`.
- A real copied five-paper run also tested the new DFT correction workflow. A suspicious citation-like candidate was proposed for unit correction and approved, but it remained blocked by `adsorbate_looks_like_reference` and `potential_value_outside_typical_range`; after rejecting it and verifying one real `reaction_barrier = 0.87 eV` row, ML export still reported 1 eligible / 30 blocked. Report: `backend/tests/data/codex_graphite_defects_e2e/runtime_verify/20260604T050556Z/dft_review_correction_report.json`.
- Per-item Codex checks succeeded for every paper with DFT results and every paper with figures.
- DFT settings extraction found evidence for VASP, PBE, PAW, cutoff, and k-points in 1710.10084.
- The `Gaussian smearing` false positive found during the first E2E run was fixed; 1710.10084 now reports VASP as the only software candidate.
- Figure extraction is not globally broken. Sampled figures from 1710.10084, 2308.05425, and 1112.5598 were readable. Some small crops in 1405.1928 and 1112.5598 were flagged as `small_crop_or_subfigure`.
- Local GROBID was unavailable in this run. The app failed fast and continued with Docling/text parsing, which is acceptable for an offline workbench.

## Next Steps

1. Add bulk queue actions for suspicious DFT candidates, so Codex can triage repeated noisy rows faster.
2. Add more negative rules for DFT result/settings extraction as new real-paper false positives appear.
3. Put external web AI analysis imports into the same candidate/review model so mechanism knowledge no longer collapses to blank fields.
4. Add richer paper-level citation filters, including journal quality metadata, year windows, topic tags, and evidence-backed relevance.
