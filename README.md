# AI-shujvku

这是个人科研工具仓库。目前只保留一个活跃系统：`literature-ai`（文献 AI 工具台）。本文件是仓库唯一主 README，也是新协作者的默认入口。

## 系统定位

`literature-ai` 面向 Codex / IDE AI 的本地文献工具台，负责文献采集、PDF 解析、证据检索、候选结构化数据、审阅队列和受控导出。软件负责准备材料和维护受控流程；最终阅读、核对、归纳、写作和确认由 Codex 或人工完成。

系统默认不把任何 AI 输出当作最终事实。

## 当前稳定基线（2026-06-27）

- **数据库**：`PostgreSQL + pgvector` 是唯一且默认的活跃业务数据源。
- **MCP 协作面**：MCP 是 IDE AI 的首选受控协作入口；HTTP MCP 必须使用配置好的 Bearer key。
- **IDE 后备路径**：若当前 IDE 会话未暴露 MCP 工具，可改走 `literature-ai/backend` 中 `app.mcp.context.mcp_auth_context` + `app.mcp.server` 的仓库内后备路径。
- **服务暴露**：Docker 默认暴露本机 `8000` Owner 网关，以及 `8080` 只读分享网关；数据库和内部服务不直接暴露到 LAN。
- **DFT / project-library**：DFT 抽取结果默认只是候选，必须经过证据、审核、材料绑定和导出安全门。
- **本地产物边界**：`local/`、`literature-ai/outputs/tmp/`、`literature-ai/outputs/exports/`、`test-results/`、`.pytest_cache/` 和临时 scratch 脚本不属于源码，不应作为正式提交内容。

## 快速启动

```bash
cd literature-ai
cp .env.example .env
# 把 .env 中的占位 secret 改成真实值后再启动
docker compose up --build
curl http://localhost:8000/api/health
```

主工作台：<http://localhost:8000/pages/literature_library/index.html>

## 主要目录

```text
AI-shujvku/
  README.md                ← 仓库唯一主 README
  literature-ai/           ← 唯一活跃系统
    AGENTS.md              ← AI 协作者规则
    backend/               ← FastAPI 后端、解析管线、MCP 服务
    frontend/              ← 静态工作台页面与前端测试
    prompts/               ← 提取、审核、写作协议
    docs/                  ← 当前文档索引、MCP 文档、schema、plans/audits
    deploy/                ← 部署配置
    data/                  ← 运行期数据与存储根
    outputs/               ← 系统运行期导出目录
    deliverables/          ← 需要保留的交付快照与受控导出
  scripts/                 ← 仓库级运维/清理脚本
  local/                   ← 本地备份、测试样本、回归运行结果
```

说明：

- `literature-ai/` 是唯一系统根目录。
- 根目录只保留仓库入口、运维脚本和 `local/` 本地资产区；业务源码统一留在 `literature-ai/`。
- `local/` 仅存放本机备份、测试样本和验收运行结果；需要保留进仓库的正式产物应放入 `literature-ai/deliverables/`。

## 文档分工

| 文档 | 作用 |
|------|------|
| [literature-ai/AGENTS.md](./literature-ai/AGENTS.md) | AI 协作者规则、数据安全边界、文档同步原则 |
| [literature-ai/docs/README.md](./literature-ai/docs/README.md) | 当前文档索引、有效基线和历史文档边界 |
| [literature-ai/docs/mcp/MCP_API.md](./literature-ai/docs/mcp/MCP_API.md) | MCP API 与工具说明 |
| [literature-ai/README.md](./literature-ai/README.md) | `literature-ai/` 目录落点说明；不再承载完整主说明 |

如果这些文档出现冲突，以本文件、`literature-ai/AGENTS.md`、当前代码行为和测试结果为准。

## 运行与提交边界

- 不要提交本地 token、数据库连接串、临时探针脚本或本地调试输出。
- 根目录下的 `local/` 与 `literature-ai/outputs/tmp/`、`literature-ai/outputs/exports/`、`test-results/`、`.pytest_cache/` 默认按“可清理或本地保留产物”处理。
- 如 IDE 会话缺少 MCP 工具，优先走仓库内受控后备路径，不要绕过权限边界直接操作 service、session、model 或数据库。

## 给新协作者的提醒

1. 先读 [literature-ai/AGENTS.md](./literature-ai/AGENTS.md)。
2. 以 `git status`、`git log` 和当前代码/测试结果为准，不要依赖旧计划文档猜测现状。
3. PostgreSQL 是唯一真源；如文档与代码冲突，优先修正文档，不要编造“已经完成”的迁移结论。
