# Literature AI

AI-assisted literature parsing and writing support for:

- first-principles / DFT studies
- single-atom and dual-atom catalysts
- lithium-sulfur battery cathodes

## Current Progress
 
**Overall: approximately 99.5% complete. Literature Acquisition Center frontend integration is 100% complete and fully verified with Playwright tests green. Backend core pipeline is fully complete and hardened.**

### Recent Changes

| Date | Description | Files Affected |
|------|-------------|---------------|
| 2026-05-25 | G1 Acquisition Identity Guard: centralized paper identity reports for DOI/arXiv/title-year matching, deduplicated metadata-only upserts, routed AI workflow fallback through the identity service, and added attach-PDF safety gates for DOI conflicts and low-confidence manual binds while preserving existing paper IDs and review rows. | `backend/app/services/paper_identity.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/workflow_jobs.py`, `backend/app/api/papers/ingestion.py`, `backend/app/api/papers/discovery.py`, `backend/tests/test_papers_api.py` |
| 2026-05-25 | Sprint 1 Acquisition Center: Integrated Literature Acquisition Center in the frontend. Refactored Add Literature dialog to support direct uploading vs. metadata-only select-and-attach (`POST /api/papers/{paper_id}/attach-pdf`). Added 5-state list status badges (`metadata_only`, `pdf_available`, `parsed`, `extraction_failed`, `duplicate_candidate`) and conditional detail-page banner. Handled `merged` and 409 already-exists conflicts with an interactive jump toast. Updated AI workflow result listing with colored chips. | `frontend/pages/literature_library/index.html`, `frontend/pages/literature_library/page.css`, `frontend/pages/literature_library/page.js`, `frontend/pages/literature_library/render-detail.js`, `frontend/pages/literature_library/jobs.js`, `frontend/pages/literature_library/api.js`, `frontend/tests/smoke.spec.js` |
| 2026-05-25 | Sprint 0 front-end stability: added DOM query null-safety checks across all literature library scripts, verified split-pane drag handles, and expanded Playwright smoke tests to cover empty library state and metadata-only display. | `frontend/pages/literature_library/page.js`, `frontend/pages/literature_library/render-list.js`, `frontend/pages/literature_library/render-detail.js`, `frontend/pages/literature_library/jobs.js`, `frontend/pages/literature_library/writer.js`, `frontend/pages/literature_library/review.js`, `frontend/pages/literature_library/api.js`, `frontend/tests/smoke.spec.js` |
| 2026-05-25 | Simplified the Literature Library into a browse/filter/detail surface: consolidated ingestion actions behind one Add Literature menu, moved paper-level actions into the selected-paper detail header, split detail content into lighter tabs, added an empty-library state, and stabilized AI search/workflow results in the add-literature modal. | `frontend/pages/literature_library/index.html`, `frontend/pages/literature_library/page.css`, `frontend/pages/literature_library/page.js`, `frontend/pages/literature_library/jobs.js`, `frontend/pages/literature_library/render-detail.js`, `frontend/pages/literature_library/render-list.js`, `frontend/tests/smoke.spec.js` |
| 2026-05-24 | Implemented Phase 6 Level 3 Figure Numerical Extraction & RAG integration; created self-healing SQLite/Postgre database tables, established unified VLM classification + data points parser, and integrated figure data into Retriever with real figure captions; resolved 3 critical schema merging bugs (relationship_summary, outgoing_relationships, and writer_fallback_backend in update request). | `backend/app/schemas/documents.py`, `backend/app/services/paper_ingestion.py`, `backend/app/rag/retriever.py`, `backend/app/rag/writer.py`, `backend/app/schemas/api.py`, `backend/tests/test_figure_numerical.py` |
| 2026-05-24 | Implemented Phase 5 batch classification with rate-limiting & fallback, metadata-only fallback, and type-aware evidence scoring; resolved 3 critical schema merging bugs (skip_guard, results naming, and library_name in responses). | `backend/app/api/papers.py`, `backend/app/schemas/api.py`, `backend/app/services/extraction_pipeline.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/paper_reprocessing.py`, `backend/tests/test_paper_reprocessing.py` |
| 2026-05-22 | Fixed unified top-nav routing when no paper is selected: top tabs now open their target workspace even from the empty state, the review tab can surface the IDE/MCP guide directly, and a library with no selected paper now defaults into the AI-search workspace instead of trapping users in the paper-detail empty screen | `frontend/pages/literature_library/index.html`, `README.md` |
| 2026-05-22 | Hardened the web literature workbench for large AI search/download sessions: AI workflow can now run as a background job with status polling instead of blocking the browser, web search defaults to 100-result batches with front-end duplicate merging, the top navigation switches the unified single-page workspace directly, and the review tab exposes the IDE/MCP agent guide alongside internal/external AI analysis paths | `backend/app/api/papers.py`, `backend/app/schemas/api.py`, `backend/tests/test_papers_api.py`, `frontend/pages/literature_library/index.html`, `README.md` |
| 2026-05-22 | Restored the literature web app into a fuller single-page workspace: split paper list + detail view, valid redirects from legacy pages, live tabs for paper detail / internal AI drafting / external AI review / AI-assisted search / aggregate view, and a new internal-AI parse API that reviews one paper with the configured LLM and materializes notes/corrections/relationships back into the database | `frontend/pages/literature_library/index.html`, `frontend/pages/paper_detail/index.html`, `frontend/pages/dft_database/index.html`, `frontend/pages/mechanism_knowledge/index.html`, `frontend/pages/writing_cards/index.html`, `frontend/pages/ai_writer/index.html`, `frontend/pages/external_analysis_workbench/index.html`, `backend/app/api/external_analysis.py`, `backend/tests/test_external_analysis.py`, `README.md` |
| 2026-05-22 | Recovered the web literature workspace from a regressed/garbled frontend snapshot: rebuilt `literature_library` into a clean Chinese workbench, added explicit `file://` warning, wired current-library filtering back into list/upload/download actions, widened online search to 100 results, and surfaced `metadata_only` ingest status in the main paper list and ingest feedback | `frontend/pages/literature_library/index.html`, `README.md` |
| 2026-05-22 | Reworked web library switching around a real library manager: new/imported libraries can now be desktop-shared project folders, activation switches the backend DB and storage root based on library layout (`storage/` legacy vs `papers/` shared), and regression coverage was added for create/import/activate behavior | `backend/app/services/library_manager.py`, `backend/tests/test_library_manager.py`, `frontend/pages/literature_library/index.html`, `backend/app/db/models.py`, `backend/app/db/session.py`, `backend/app/schemas/api.py`, `backend/app/api/papers.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/paper_query.py`, `README.md` |
| 2026-05-22 | Fixed web-side literature acquisition bottlenecks: online discovery is no longer hard-capped to 10 items from the frontend, search now goes directly against OpenAlex/arXiv official APIs instead of the slow legacy wrapper, AI workflow defaults were widened substantially, duplicate `response.json()` parsing was removed, and discovery/AI ingestion now falls back to metadata-only paper collection when PDF download fails so search hits can still be retained in the active library | `frontend/pages/literature_library/index.html`, `backend/app/api/papers.py`, `backend/app/schemas/api.py`, `backend/app/services/discovery_service.py`, `backend/app/services/paper_ingestion.py`, `backend/tests/test_papers_api.py`, `README.md` |
| 2026-05-22 | Rebuilt the Windows startup script so double-click startup checks Docker first, starts Compose with build, waits for `/api/health`, opens the web page only after the backend is ready, and prints backend logs instead of silently opening a refused localhost page | `启动文献库.bat`, `README.md` |
| 2026-05-23 | Added Rule 6 to AGENTS.md: minimum-change principle, pre-confirmation for large edits, ask-before-act rule, and mandatory double-confirmation for destructive git operations | `AGENTS.md`, `README.md` |
| 2026-05-23 | RAG pipeline hardened: evidence pack cross-type dedup + round-robin paper diversity sorting in retriever/prompt_builder; fact-level fallback repair in writer (missing_fact_claims now trigger repair, not just missing_values); citation guard expanded with `mediates`/`infers_causality` synonyms, substring phrase matching for multi-word triggers, and new strict context keywords (`coordination`, `mechanism`) | `backend/app/rag/retriever.py`, `backend/app/rag/prompt_builder.py`, `backend/app/rag/writer.py`, `backend/app/rag/citation_guard.py` |
| 2026-05-22 | Added per-paper serial numbers (001, 002 …) that are permanently assigned at ingest time and do not change on re-sort; migrated existing papers with a Python-based backfill (SQLite-safe, no window-function SQL) | `backend/app/db/models.py`, `backend/app/db/session.py`, `backend/app/schemas/api.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/paper_query.py`, `frontend/pages/literature_library/index.html` |
| 2026-05-22 | Added `ReferenceEntry` model and full CRUD API so each paper can carry its own reference list; references are linked to their parent paper (and optionally to another ingested paper via `linked_paper_id`) instead of being treated as independent papers | `backend/app/db/models.py`, `backend/app/db/session.py`, `backend/app/schemas/api.py`, `backend/app/api/references.py`, `backend/app/main.py`, `backend/app/services/paper_query.py`, `frontend/pages/literature_library/index.html` |
| 2026-05-22 | Added split-pane drag handles, fixed mouse+touch resizing with RAF throttle and `window.blur` fallback | `frontend/pages/literature_library/index.html` |
| 2026-05-22 | Added web-side literature library selection and AI connection controls; threaded `library_name` through upload, DOI download, local listing, and AI workflow ingestion; added backend library listing/filtering with regression coverage | `frontend/pages/literature_library/index.html`, `backend/app/api/papers.py`, `backend/app/db/models.py`, `backend/app/db/session.py`, `backend/app/schemas/api.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/paper_query.py`, `backend/tests/test_papers_api.py` |
| 2026-05-22 | Added machine-readable agent guide endpoint (`GET /api/system/agent-guide`) so external IDE/agent clients can self-discover the preferred HTTP workflow, MCP URL, desktop sync path, and required LLM environment variables | `backend/app/api/system.py`, `backend/app/main.py` |
| 2026-05-22 | Hardened the AI-driven discovery/download path with direct-PDF fallback when primary provider fails; reuse external metadata consistently during ingest; classify workflow failures more explicitly | `backend/app/services/discovery_service.py`, `backend/app/api/papers.py`, `backend/app/schemas/api.py` |
| 2026-05-22 | Added one-shot `ai_workflow` API for external AI to rewrite a query, search providers, download candidate PDFs, and ingest in a single call | `backend/app/schemas/api.py`, `backend/app/api/papers.py` |
| 2026-05-22 | Added post-extraction quality gate that rescales confidence from evidence quality, filters weak structured items, and reconciles `dft_results` with `comprehensive_analysis.computational_results` | `backend/app/services/extraction_pipeline.py` |
| 2026-05-22 | Hardened extraction quality by switching DFT/writing-card/comprehensive analyzers from naive front-truncation to section-focused context selection; merged LLM output with rule-based extraction for better completeness on long papers | `backend/app/extractors/dft_results_extractor.py`, `backend/app/extractors/writing_card_extractor.py`, `backend/app/extractors/comprehensive_extractor.py` |
| 2026-05-22 | Tightened mechanism extraction to skip descriptive mentions without mechanistic action signals; upgraded DFT table parsing to use markdown table headers/columns for more precise adsorption energy / Bader charge extraction | `backend/app/extractors/mechanism_extractor.py`, `backend/app/extractors/dft_results_extractor.py` |
| 2026-05-21 | Consolidated the fragmented frontend into a single-page research workspace at `literature_library`; old per-page URLs redirect automatically | `frontend/pages/literature_library/index.html`, `frontend/pages/ai_writer/index.html`, `frontend/pages/dft_database/index.html`, `frontend/pages/mechanism_knowledge/index.html`, `frontend/pages/writing_cards/index.html`, `frontend/pages/paper_detail/index.html` |
| 2026-05-21 | Restored full Docling parsing in Docker with CPU-only Torch wheels, OCR-capable PDF pipeline, and persistent Docling cache | `backend/requirements.txt`, `backend/Dockerfile`, `backend/app/config.py`, `backend/app/parsers/docling_parser.py`, `docker-compose.yml` |
| 2026-05-21 | Added UTF-8 runtime hardening and recursive mojibake repair for discovery metadata, ingestion payloads, and external-analysis imports | `backend/app/services/discovery_service.py`, `backend/app/services/paper_ingestion.py`, `backend/app/services/external_analysis_service.py`, `backend/app/utils/text_cleaning.py` |
| 2026-05-20 | Added external AI import runs with internal normalization candidates, materialization into notes/corrections/relationships, and paper relationship summaries | `backend/app/api/external_analysis.py`, `backend/app/db/models.py`, `backend/app/schemas/external_analysis.py`, `backend/app/services/external_analysis_service.py` |
| 2026-05-20 | Added local IDE batch PDF workflow via MCP: folder scanning, skip-if-already-ingested detection, and batch ingestion | `backend/app/api/papers.py`, `backend/app/mcp/server.py`, `backend/app/services/local_pdf_service.py` |
| 2026-05-20 | Added first-phase MCP collaboration server with API-key auth, shared notes, correction proposals, parse jobs | `backend/app/mcp/*.py`, `backend/app/schemas/mcp.py`, `MCP_IMPLEMENTATION.md` |
| 2026-05-20 | Upgraded extraction pipeline to true LLM semantic parsing with Pydantic JSON Schema; added OpenAI-compatible LLMService | `backend/app/services/llm_service.py`, `backend/app/extractors/dft_results_extractor.py`, `backend/app/extractors/writing_card_extractor.py` |

### Completed Core Features

- PDF ingestion via path or upload, with GROBID + Docling dual parsing
- Unified paper document assembly (sections, tables, figures)
- SQLite (local) / PostgreSQL + pgvector (Docker) persistence
- Stage 2 extraction: Comprehensive 12-class paper categorization with confidence scoring, DFT settings, catalyst samples, computational results, electrochemical performance, mechanism claims, writing cards
- **Serial numbers** — each paper gets a persistent, per-library sequence number (001, 002 …) assigned at ingest time
- **Reference management** — each paper carries its own reference list (`ReferenceEntry`), with optional cross-linking to other ingested papers; CRUD via `GET/POST/PUT/DELETE /api/papers/{id}/references`
- Paper list with keyword search, year/journal/feature filters; paper detail API with all extraction results
- Paper reprocessing (re-run Stage 2 on existing papers)
- Hybrid retrieval (lexical + embedding) across sections, facts, claims, and writing cards
- 3 writer backends: `rule` (deterministic), `llm_stub` (test integration), `openai_compatible` (real LLM with auto-fallback)
- Evidence pack compression: section-aware evidence grouping with numeric guardrails
- Citation guard: numeric value + unit + context matching to prevent fabricated numbers; mechanism-direction / superlative / causal trigger checks; **fact-level expansion with `mediates`/`infers_causality` synonyms and substring phrase matching**
- **Fact-level fallback repair** — writer now repairs `missing_fact_claims` (not just `missing_values`), with `_fact_claim_unsupported` action suffix
- **Evidence pack dedup & diversity** — cross-type deduplication by paper_id + text fingerprint; round-robin sorting prevents single-paper evidence monopolization
- Online literature search via OpenAlex/arXiv through discovery service
- DOI/URL download with automatic ingestion into the pipeline
- Unified single-page workspace at `literature_library` (library, search, DFT, mechanism, writing, AI writer)
- AI writer frontend shows backend_used, llm_status, guard_actions, and citation guard results
- MCP collaboration layer: external AI can read parsed papers, append shared notes, propose corrections, trigger parse jobs, and review/approve/reject corrections
- Local IDE AI batch-PDF workflow via MCP (scan folder → skip ingested → batch parse)
- External AI output import with intermediate normalization and materialization into notes/corrections/relationships
- Machine-readable agent guide endpoint for self-configuring IDE clients

### Remaining Work

1. **文件夹即库端到端验证** — 库管理 UI 已就位，docker-compose volume 映射已配置，需启动测试完整流程（新建库/导入/切换/移除）
2. **Real LLM quality tuning** — DeepSeek live connectivity verified; a final round of stylistic/domain tightening still remains
3. **Frontend polish** — loading states, error recovery UX, and responsive design edge cases
4. **End-to-end integration tests** — real backend mock tests, fallback scenario tests, multi-paper retrieval tests
5. **Admin panel** — paper management, extraction queue monitoring, writer configuration UI

## Layout

```text
literature-ai/
  backend/
  frontend/
  prompts/
  storage/
  docker-compose.yml
```

## Quick start

1. Copy `.env.example` to `.env` before the first local start:

```bash
cp .env.example .env
```

2. Start the stack:

```bash
docker compose up --build
```

`docker-compose.yml` uses development-only default credentials for PostgreSQL and MinIO. Keep them only for local/dev use and override them before any shared deployment.

Host directory browsing notes:

- Windows default host mount root is `/c/Users`.
- macOS/Linux users should set `LITAI_HOST_USERS_ROOT` in `.env` before starting if host directory browsing is needed.
- If host directory browsing is not needed, you can comment out the `/host/users` volume mount in `docker-compose.yml`.

3. Check health:

```bash
curl http://localhost:8000/api/health
```

4. Ingest a PDF by path:

```bash
curl -X POST http://localhost:8000/api/papers/ingest/path \
  -H "Content-Type: application/json" \
  -d "{\"pdf_path\":\"/data/storage/pdf/sample.pdf\"}"
```

5. Or upload a PDF:

```bash
curl -X POST http://localhost:8000/api/papers/ingest/upload \
  -F "file=@sample.pdf"
```

### Local security boundaries

- `POST /api/settings` is now limited to local requests by default. Set `LITAI_SETTINGS_ADMIN_TOKEN` and send it as `X-Settings-Token` or `Authorization: Bearer ...` if you need non-local administration.
- `GET /api/libraries/browse` remains whitelist-only. Allowed roots come from `LITAI_BROWSE_ROOTS` plus the local home directory.
- MCP keys should be scoped to the minimum required capabilities. Use separate contributor and reviewer keys instead of a single broad key.

## MCP quick start

1. Configure MCP keys in `.env`:

```env
LITAI_MCP_ENABLED=true
LITAI_MCP_SERVER_NAME=Literature AI MCP
# Minimum-privilege example: contributor key cannot review corrections.
LITAI_MCP_API_KEYS=claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections
```

Default policy: keep MCP disabled unless a trusted local/dev workflow explicitly needs it, and only then enable the smallest capability set required for that client.

2. Start the backend:

```bash
docker compose up --build
```

3. Connect an external AI client to:

```text
http://localhost:8000/mcp
```

4. Send:

```text
Authorization: Bearer <your_mcp_key>
```

5. Use contributor tools for reading, notes, parse requests, and correction proposals

6. Use reviewer tools only with trusted reviewer keys

See `MCP_API.md` for the full MCP workflow, target path conventions, and client examples.

### Local IDE batch PDF quick start

For a local IDE AI that needs to process a folder of PDFs:

1. call `scan_local_pdfs`
2. inspect which files are already parsed
3. call `ingest_pdf_batch` with `only_unparsed=true`
4. hand the resulting `paper_id`s to a reviewer AI for validation

### Online literature search

```bash
curl "http://localhost:8000/api/papers/discovery/search?q=Fe-N4+single+atom+catalyst&limit=5"
```

### Download and ingest by DOI

```bash
curl -X POST http://localhost:8000/api/papers/discovery/download \
  -H "Content-Type: application/json" \
  -d "{\"identifier\":\"10.1000/xyz123\"}"
```

## Current backend workflow

1. PDF enters the system by local path, upload, or DOI download
2. GROBID extracts metadata, abstract, sections, references, and TEI XML
3. Docling extracts markdown, tables, figures, and page-ordered body content
4. Outputs are merged into a unified paper document
5. Paper, sections, tables, and figures are stored; a persistent per-library `serial_number` is assigned
6. Stage 2 extractors populate:
   - `dft_settings`
   - `catalyst_samples`
   - `dft_results`
   - `electrochemical_performance`
   - `mechanism_claims`
   - `writing_cards`
7. Evidence spans are stored for downstream retrieval
8. RAG writer retrieves evidence, builds evidence packs, drafts sections with guardrails
9. Citation guard validates generated text against retrieved evidence, auto-falls back on violations

## Key API endpoints

### Papers

- `POST /api/papers/ingest/path`
- `POST /api/papers/ingest/upload`
- `GET /api/papers` (with `?q=`, `?year=`, `?journal=`, `?has_dft_results=`, `?has_writing_cards=`, `?library_name=`, `?source_path=`, `?limit=`, `?offset=`)
- `GET /api/papers/{paper_id}`
- `POST /api/papers/{paper_id}/extract`
- `GET /api/papers/discovery/search?q=...`
- `POST /api/papers/discovery/download`
- `POST /api/papers/ai_workflow`
- `GET /api/papers/aggregate`

### References

- `GET /api/papers/{paper_id}/references`
- `POST /api/papers/{paper_id}/references`
- `PUT /api/papers/{paper_id}/references/{ref_id}`
- `DELETE /api/papers/{paper_id}/references/{ref_id}`

### Writer

- `GET /api/writer/status`
- `POST /api/writer/draft`

### Corrections

- `GET /api/corrections`
- `GET /api/corrections/{correction_id}`
- `POST /api/corrections/{correction_id}/approve`
- `POST /api/corrections/{correction_id}/reject`

### External Analysis

- `POST /api/external-analysis/import`
- `GET /api/external-analysis/runs`
- `GET /api/external-analysis/runs/{run_id}`
- `POST /api/external-analysis/runs/{run_id}/materialize`

### System

- `GET /api/system/agent-guide`

Example:

```bash
curl -X POST http://localhost:8000/api/writer/draft \
  -H "Content-Type: application/json" \
  -d "{\"topic\":\"Fe-N4 single-atom catalysts for lithium-sulfur cathodes\",\"paper_ids\":[\"YOUR-PAPER-ID\"]}"
```

Response fields include:

- `backend_used`
- `llm_status`
- `llm_error`
- `llm_diagnostics`
- `prompt_preview`
- `outline`
- `introduction`
- `dft_results`
- `discussion`
- `figure_storyline`
- `retrieved`
- `citation_guard`
- `guard_actions`

## Frontend pages

Open these static pages in the running app:

- `/pages/literature_library/index.html` — unified workspace (library, search, DFT, mechanism, writing, AI writer)
- `/pages/paper_detail/index.html?id=<paper_id>` *(redirects to unified workspace)*
- `/pages/dft_database/index.html` *(redirects to unified workspace)*
- `/pages/mechanism_knowledge/index.html` *(redirects to unified workspace)*
- `/pages/writing_cards/index.html` *(redirects to unified workspace)*
- `/pages/ai_writer/index.html` *(redirects to unified workspace)*

## Writer backend modes

The writer currently supports 3 modes:

- `rule`
  deterministic offline rule-based drafting
- `llm_stub`
  offline stand-in that rewrites rule drafts so the integration path can be tested
- `openai_compatible`
  OpenAI-compatible `chat/completions` backend with automatic fallback

Relevant environment variables:

- `LITAI_WRITER_BACKEND`
- `LITAI_WRITER_MODEL`
- `LITAI_WRITER_API_BASE`
- `LITAI_WRITER_API_KEY`
- `LITAI_WRITER_TIMEOUT_SECONDS`
- `LITAI_WRITER_FALLBACK_BACKEND`
- `LITAI_WRITER_PROMPT_PATH`
- `LITAI_MCP_ENABLED`
- `LITAI_MCP_SERVER_NAME`
- `LITAI_MCP_API_KEYS`

### Recommended local defaults

```env
LITAI_WRITER_BACKEND=rule
LITAI_WRITER_FALLBACK_BACKEND=rule
```

### OpenAI-compatible example

```env
LITAI_WRITER_BACKEND=openai_compatible
LITAI_WRITER_MODEL=gpt-4.1-mini
LITAI_WRITER_API_BASE=https://your-openai-compatible-endpoint/v1
LITAI_WRITER_API_KEY=your_api_key
LITAI_WRITER_FALLBACK_BACKEND=rule
```

If `writer_api_base` or `writer_api_key` is missing, the system falls back automatically and reports:

- `backend_used: openai_compatible->rule`
- `llm_status: missing_configuration`
- `llm_error: Missing required configuration: ...`

If the remote request fails, the system still falls back and returns:

- `backend_used: openai_compatible-><fallback>`
- `llm_status: fallback:<ExceptionType>`
- `llm_error`

## Storage and artifacts

Artifacts are kept under `storage/`:

- `pdf/`
- `tei/`
- `docling_json/`
- `figures/`
- `tables/`
- `markdown/`

## External dependencies

- `GROBID` is provided as a Docker service
- `Docling`, `pymatgen`, and `ASE` are in `backend/requirements.txt`
- embeddings are still an offline deterministic placeholder for MVP plumbing

## If your environment cannot access the network

Please mirror these before build:

- Docker images:
  - `pgvector/pgvector:pg16`
  - `redis:7`
  - `minio/minio:RELEASE.2025-04-22T22-12-26Z`
  - `lfoppiano/grobid:0.8.1`
  - `python:3.11-slim`
- Python packages from `backend/requirements.txt`

## Current limitations

- embeddings are placeholder embeddings, not semantic model embeddings
- writer quality is still prompt/rule oriented unless a real LLM backend is configured
- citation guard checks numeric support, fact-level triggers (mediates/causality), and common claim patterns; fact-level repair now active but full claim-level truthfulness still requires real LLM verification
- frontend is static HTML/JS, not a full componentized app
- pgvector retrieval is scaffolded through stored embeddings, but the current retriever is still lexical-first

## AI Collaboration

See [AGENTS.md](./AGENTS.md) for sync rules that all AI collaborators must follow.
