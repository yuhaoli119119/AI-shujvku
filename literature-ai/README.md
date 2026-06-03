# Literature AI

`literature-ai` 是一个面向科研文献处理的本地化工作台，包含文献采集、PDF 解析、结构化抽取、外部 AI 审阅和 MCP 协作接口。

## 当前定位

- 当前活跃业务数据库：每个文献库目录下的 `SQLite database.sqlite`
- `PostgreSQL + pgvector`：保留为可选能力，不是默认活跃库
- 当前阶段：`D2 数据底座 / migration readiness`
- 当前不应直接执行 migration apply

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

## 快速启动

```bash
docker compose up --build
curl http://localhost:8000/api/health
```

主工作台：

- <http://localhost:8000/pages/literature_library/index.html>

## 文档说明

- `docs/mcp/`：当前仍有效的 MCP 文档
- `docs/archive/`：历史计划、旧报告、旧变更记录

如果你要协作修改本项目，请先读 `AGENTS.md`，不要把 archive 文档当作当前执行规范。
