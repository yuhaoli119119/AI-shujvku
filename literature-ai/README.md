# Literature AI

`literature-ai` is a local literature workbench for AI-assisted reading, parsing, evidence review, data curation, and writing support. It collects papers, parses PDFs, stores traceable candidates, exposes review queues, and provides MCP tools for IDE-based AI collaboration.

The application does not treat any AI output as final truth by default.

## Current Position

- The software is the workbench: it handles paper intake, PDF parsing, artifact preparation, evidence retrieval, candidate extraction, queues, and guarded export.
- AI roles are assigned per task by the user. Codex, Gemini, GLM, Claude, or another IDE AI may parse, inspect figures, audit DFT data, summarize evidence, or perform a second pass.
- Model names do not grant trust. All AI outputs remain candidates until they pass the required evidence, review, and confirmation gates.
- PostgreSQL with pgvector is the sole business and test database.
- MCP is the preferred controlled collaboration surface for IDE AI workers and other clients.
- HTTP MCP always requires a configured Bearer key; private-network source addresses do not grant capabilities. Repository-native `mcp_auth_context` remains the in-process fallback.
- Docker exposes a loopback-only Owner gateway on port 8000 and a separate LAN read-only share gateway on port 8080. PostgreSQL, Redis, MinIO, Grobid, and the backend service are not directly exposed to the LAN.
- Bulk exports are disabled by default with `LITAI_EXPORTS_ENABLED=false`; export and share-link creation use independent MCP capabilities.
- If the current IDE session does not expose MCP tools, the repository-native backend path in `backend/` may be used as the fallback execution route via `app.mcp.context.mcp_auth_context` and `app.mcp.server`.

## Stable Scope as of 2026-06-27

- The DFT extraction and review path is usable, but it is intentionally guarded: extracted DFT values are candidates until evidence, review state, material binding, and export safety checks agree.
- Literature Library groups DFT candidate cards by catalyst sample / active-site identity. Existing per-row review actions still operate inside those groups.
- Catalyst sample identity from DFT rows and catalyst basic information from the catalyst extractor now converge on the same `CatalystSample` record when a single explicit DFT catalyst is present.
- Project-library v4 export remains conservative. Single-fact or conflicting sample records may be blocked even if individual DFT rows look valid.
- `potential_determining_step` table labels are context, not numeric DFT results, and should not create `DFTResult` candidates with empty values.
- Local generated artifacts under `outputs/tmp/`, `outputs/exports/`, `test-results/`, `.pytest_cache/`, and ad-hoc scratch scripts are not source code and should not be uploaded.

## Main Components

- `backend/`: FastAPI backend, parsing pipeline, extraction services, MCP server.
- `frontend/`: static workbench pages.
- `prompts/`: extraction, audit, and writing protocols.
- `data/`: Docker-mounted runtime data, including `library_registry.json`, `libraries/`, and `storage/`.
- `storage/`: legacy directory; current runtime artifacts should use `data/storage/`.
- `docs/`: current docs, MCP docs, baseline, plans, and historical audit records.

## Entry Points

- Root guidance: [../README.md](../README.md)
- AI collaboration rules: [AGENTS.md](./AGENTS.md)
- Chinese usage guide: [使用说明.md](./%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md)
- Documentation index: [docs/README.md](./docs/README.md)
- MCP API: [docs/mcp/MCP_API.md](./docs/mcp/MCP_API.md)

## Quick Start

```bash
cp .env.example .env
# Replace every secret placeholder before starting.
docker compose up --build
curl http://localhost:8000/api/health
```

Main workbench:

- <http://localhost:8000/pages/literature_library/index.html>

AI paper context bundle:

- HTTP: `GET /api/papers/{paper_id}/codex-context`
- MCP: `get_codex_context`

The tool name still uses `codex-context` for compatibility, but the bundle is available to any assigned AI reviewer or parser.

If MCP tools are unavailable in the current IDE session, use the backend-native `app.mcp.*` path from `backend/` instead of stopping at tool-missing.

## Documentation Rules

- `docs/README.md`, `docs/mcp/`, this README, `AGENTS.md`, and `使用说明.md` describe the current architecture.
- `docs/plans/` and `docs/audits/` contain mixed historical and current planning records. If they conflict with the current baseline, follow the baseline.
- Files and code that still use names such as `GeminiAuditService`, `gemini_audit_protocol`, or `Codex context` may be compatibility names. They do not imply fixed model ownership.

## Verification Gate for DFT / Project-Library Changes

Use targeted tests before uploading changes:

```powershell
cd literature-ai/backend
python -m pytest -q tests/test_dft_results_extractor.py tests/test_extraction_pipeline.py::test_stage2_preserves_distinct_catalyst_identity_for_equal_dft_values tests/test_extraction_pipeline.py::test_stage2_merges_dft_catalyst_identity_with_extractor_basic_info tests/test_catalyst_basic_info_api.py
python -m compileall -q app tests

cd ../frontend
npx playwright test tests/smoke.spec.js -g "DFT|dft|project library|literature library DFT"
npx playwright test tests/dft_ml_dataset.spec.js
```
