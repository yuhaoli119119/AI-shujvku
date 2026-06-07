# Literature AI

`literature-ai` 是一个面向 Codex 的本地文献工具台，包含文献采集、PDF 解析、结构化候选抽取、外部解析导入和 MCP 协作接口。它不再以网页内 AI 自动给出最终结论为核心，而是为 Codex 提供可读、可查、可核对的文献资料底座。

## 当前定位

- Codex 是主分析者：阅读、筛选、核对、归纳、写作和数据整理由 Codex 或人工完成
- 软件是工具台：负责文献入库、PDF 转换、证据检索、候选结构化数据和导出
- 网页内 AI / 自动解析：只作为辅助候选，不作为最终可信结论
- **当前核心数据库**：`PostgreSQL + pgvector` 是唯一且绝对的活跃业务数据源，SQLite 已被全面弃用。
- **27-Tool MCP 系统**：包含 `recrop_figure` 等强大工具的四层架构闭环。
- **多终端协作**：支持细粒度的基于角色的 API 权限拆分，并提供面向外部协作者的只读分享链接 (`ShareToken`)。

## 主要组件

- `backend/`：FastAPI 后端、解析管线、抽取服务、MCP 服务
- `frontend/`：静态工作台页面
- `prompts/`：抽取与写作相关提示词
- `data/`：当前 Docker 默认宿主机数据目录，包含 `library_registry.json`、`libraries/`、`storage/`
- `storage/`：历史目录；当前建议以 `data/storage/` 为准
- `docs/`：当前文档、MCP 文档、历史归档

## 入口

- 根说明：[../README.md](../README.md)
- AI 协作规则：[AGENTS.md](./AGENTS.md)
- 中文使用说明：[使用说明.md](./%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md)
- 文档索引：[docs/README.md](./docs/README.md)
- Codex 中心化重定位：[docs/plans/codex_centered_refocus.md](./docs/plans/codex_centered_refocus.md)

## 快速启动

```bash
docker compose up --build
curl http://localhost:8000/api/health
```

主工作台：

- <http://localhost:8000/pages/literature_library/index.html>

Codex 文献包：

- HTTP：`GET /api/papers/{paper_id}/codex-context`
- MCP：`get_codex_context`

## 文档说明

- `docs/mcp/`：当前仍有效的 MCP 文档
- `docs/archive/`：历史计划、旧报告、旧变更记录

如果你要协作修改本项目，请先读 `AGENTS.md`，不要把 archive 文档当作当前执行规范。
