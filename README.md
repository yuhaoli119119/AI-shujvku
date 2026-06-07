# AI-shujvku

这是个人科研工具仓库，当前核心项目是 `literature-ai`（文献 AI 工具台）。

## 核心项目：Literature AI

面向 Codex / IDE AI 的本地文献工具台，包含文献采集、PDF 解析、结构化抽取、外部解析导入和 MCP 协作接口。软件负责文献入库、PDF 转换、证据检索、候选结构化数据和导出；最终阅读、核对、归纳、写作和数据整理由 Codex 或人工完成。

**当前基线**：
- **数据库**：`PostgreSQL + pgvector` 是唯一且默认的活跃业务数据源（Docker Compose 中 `postgres` 容器必须启动）
- **MCP 工具**：27 个，覆盖提取→裁切→审核→分享的完整闭环
- **权限体系**：6 级 capability（`read_papers` / `append_notes` / `propose_corrections` / `request_parse` / `review_corrections` / `review_dft`）
- **多 AI 协作**：Blackboard 模式，AI 分析结果自动落盘为 PaperNote（"雁过留声"）
- **只读分享**：通过 `create_share_token` 生成安全链接，外部用户可查看论文/图表/DFT/审阅记录，不可修改

## 快速启动

```bash
cd literature-ai
docker compose up --build
curl http://localhost:8000/api/health
```

主工作台：<http://localhost:8000/pages/literature_library/index.html>

## 文档入口

| 文档 | 内容 |
|------|------|
| [literature-ai/AGENTS.md](./literature-ai/AGENTS.md) | AI 协作者规则与交付边界（新 AI 必读） |
| [literature-ai/README.md](./literature-ai/README.md) | 子项目技术说明 |
| [literature-ai/使用说明.md](./literature-ai/%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md) | 中文使用说明 |
| [literature-ai/docs/README.md](./literature-ai/docs/README.md) | 文档索引 |

## 仓库结构

```
AI-shujvku/
  literature-ai/       ← 核心项目（FastAPI + PostgreSQL + MCP）
    backend/           ← FastAPI 后端、MCP 服务、解析管线
    frontend/          ← 静态工作台页面
    docs/              ← 文档（mcp/ 有效，plans/ 当前计划）
  .workbuddy/          ← 工作日志与记忆
```

## 给新协作者的提醒

1. **先读 `AGENTS.md`** — 它定义了数据安全红线、权限边界和文档同步原则
2. **以 `git status` 和 `git log` 为准** — 当前真实状态以代码和提交历史为准，不要依赖已被删除的历史文档
3. **数据库是 PostgreSQL，不是 SQLite** — SQLite 文件若存在，仅为历史遗留，不作为活跃数据源
