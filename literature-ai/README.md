# Literature AI

`literature-ai` is a local literature workbench for AI-assisted reading, parsing, evidence review, data curation, and writing support. It collects papers, parses PDFs, stores traceable candidates, exposes review queues, and provides MCP tools for IDE-based AI collaboration.

The application does not treat any AI output as final truth by default.

## Current Position

- The software is the workbench: it handles paper intake, PDF parsing, artifact preparation, evidence retrieval, candidate extraction, queues, and guarded export.
- AI roles are assigned per task by the user. Codex, Gemini, GLM, Claude, or another IDE AI may parse, inspect figures, audit DFT data, summarize evidence, or perform a second pass.
- Model names do not grant trust. All AI outputs remain candidates until they pass the required evidence, review, and confirmation gates.
- PostgreSQL with pgvector is the active business database. SQLite is legacy/import/test infrastructure only.
- MCP is the preferred controlled collaboration surface for IDE AI workers and other clients.
- If the current IDE session does not expose MCP tools, the repository-native backend path in `backend/` may be used as the fallback execution route via `app.mcp.context.mcp_auth_context` and `app.mcp.server`.

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
- Current baseline: [docs/current_baseline.md](./docs/current_baseline.md)
- MCP API: [docs/mcp/MCP_API.md](./docs/mcp/MCP_API.md)

## Quick Start

```bash
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

- `docs/current_baseline.md`, `docs/mcp/`, this README, `AGENTS.md`, and `使用说明.md` describe the current architecture.
- `docs/plans/` and `docs/audits/` contain mixed historical and current planning records. If they conflict with the current baseline, follow the baseline.
- Files and code that still use names such as `GeminiAuditService`, `gemini_audit_protocol`, or `Codex context` may be compatibility names. They do not imply fixed model ownership.
