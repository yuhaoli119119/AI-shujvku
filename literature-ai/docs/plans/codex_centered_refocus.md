# Codex-Centered Refocus

## Positioning

`literature-ai` is no longer positioned as a fully automatic paper AI system that makes final scholarly judgments by itself. It is a local literature workbench for Codex.

The software should reliably collect papers, ingest PDFs, parse text/tables/figures, store traceable intermediate artifacts, index evidence, save structured candidates, and export reviewed data. Codex handles final reading, screening, verification, synthesis, writing, and data-curation decisions.

## Keep

- Batch collection: local PDFs, DOI / URL, online search, assisted batch search.
- Literature library: metadata, PDFs, sections, tables, figures, translation previews, parser artifacts.
- Codex access: HTTP and MCP access to papers, evidence, structured candidates, and notes.
- External analysis import: web AI, external models, and human notes can be imported as candidates.
- DFT data: keep the DFT candidate database and export path, but require evidence and review gates before ML use.
- Writing support: use library metadata, year, journal, impact metadata, topic filters, and evidence bundles for Codex writing.

## Downgrade

- In-page AI is no longer a final analysis authority; it is only a candidate generator to save tokens.
- Automatic parser output is no longer trusted by default; it is raw candidate material.
- Mechanism aggregation is not a primary navigation surface until evidence-backed extraction is stable.
- AI Writer is not the main writing entry point; Codex owns the writing flow, while the app provides evidence and candidate material.

## Phase 1 Changes

1. Reduce primary navigation to Library, Ingestion, DFT Database, Writing Support, and Settings.
2. Rename UI language from automatic AI review/collection to assisted parsing/search.
3. Make `/api/system/agent-guide` Codex/MCP-first.
4. Preserve backend endpoints to avoid deleting real data or breaking existing workflows.
5. Add a Codex paper context bundle for low-token, traceable single-paper reading.
6. Add Codex item context bundles for precise, low-token review of one figure, table, section, DFT candidate, mechanism claim, or writing card.
7. Surface DFT export safety in the UI so unreviewed candidates cannot be mistaken for ML-ready data.
8. Add row-level DFT verification through HTTP and MCP so evidence-backed, reviewed DFT candidates can become ML-exportable.
9. Add a DFT review queue through HTTP and MCP, with sanity flags for suspicious units, citation-like adsorbates, and abnormal magnitudes.
10. Add row-level DFT rejection through HTTP and MCP, so noisy candidates leave the active queue while staying audit-traceable and blocked from ML export.
11. Add row-level DFT correction proposals through HTTP, MCP, and the DFT database UI. Proposals enter the existing correction review queue and are not applied automatically.

## Next Priorities

1. PDF artifact quality: figure crops, captions, pages, bbox, tables, and Markdown reliability.
2. DFT candidates: add batch triage and faster approve/reject/recheck workflows on top of the queue.
3. Mechanism and writing knowledge: rebuild on evidence spans and external-analysis imports.
4. Citation and review writing: use retrieval, filters, evidence bundles, and citation metadata, then let Codex write final text.
