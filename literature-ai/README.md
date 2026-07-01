# Literature AI

`literature-ai/` 是本仓库当前唯一的活跃系统目录。仓库的唯一主 README 位于 [../README.md](../README.md)；本文件只保留为目录落点页，方便直接打开 `literature-ai/` 的协作者快速找到正确入口。

如果本文件与仓库根 README 冲突，以 [../README.md](../README.md) 为准。

## Go Here First

- Main project overview and quick start: [../README.md](../README.md)
- AI collaboration rules: [AGENTS.md](./AGENTS.md)
- Documentation index: [docs/README.md](./docs/README.md)
- MCP API and tool surface: [docs/mcp/MCP_API.md](./docs/mcp/MCP_API.md)

## Directory Role

- `backend/`: FastAPI backend, parsing pipeline, extraction services, MCP server.
- `frontend/`: static workbench pages and frontend tests.
- `prompts/`: extraction, audit, and writing protocols.
- `docs/`: current docs, MCP docs, schema notes, plans, and audit records.
- `deploy/`: deployment and gateway configuration.
- `data/`: runtime data, library registry, and storage root.
- `outputs/`: system-level exports.

## Notes

- This file is no longer a second full README.
- Use the root README for the current baseline, startup path, directory policy, and repository-level guidance.
- Use `AGENTS.md` and `docs/README.md` when you need operating rules or deeper documentation links.
