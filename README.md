# AI-shujvku

这是个人科研工具仓库。目前只保留一个活跃系统：`literature-ai`（文献 AI 工具台）。本文件是仓库唯一主 README，也是新协作者的默认入口。

## 系统定位

`literature-ai` 面向 Codex / IDE AI 的本地文献工具台，负责文献采集、PDF 解析、证据检索、候选结构化数据、审阅队列和受控导出。软件负责准备材料和维护受控流程；最终阅读、核对、归纳、写作和确认由 Codex 或人工完成。

普通 AI 输出默认只是候选、审核意见或待处理对象。只有专用 `ai_verify_content` 身份通过统一确定性门禁后才能写入 `ai_verified`；它仍不同于人工 `verified`。

## 当前稳定基线（2026-08-11）

- **数据库**：`PostgreSQL + pgvector` 是唯一且默认的活跃业务数据源。
- **MCP 协作面**：MCP 是 IDE AI 的首选受控协作入口；HTTP MCP 必须使用配置好的 Bearer key。
- **IDE 后备路径**：若当前 IDE 会话未暴露 MCP 工具，可改走 `literature-ai/backend` 中 `app.mcp.context.mcp_auth_context` + `app.mcp.server` 的仓库内后备路径。
- **服务暴露**：Docker 默认暴露本机 `8000` Owner 网关，以及 `8080` 只读分享网关；数据库和内部服务不直接暴露到 LAN。
- **DFT / project-library**：DFT 抽取结果默认只是候选，必须经过证据、审核、材料绑定和导出安全门。
- **单 AI 权威验收**：`get_ai_verification_tasks` 只读分发待验收对象，`submit_ai_verification_batch` 由一个专用验收身份提交；自动门禁无法确定处理的对象才进入 Owner-session 人工异常队列。
- **页片段恢复**：`materialize_ai_section_page_fragments` 只把服务端重新验证的页片段物化为未审核候选，不会解锁父章节或写作资格。
- **Content Knowledge 与 AI Writer**：Content Knowledge 显示对象级门禁；AI Writer 只读取有界、只读的多论文 evidence plan，每批最多 10 篇，并分别遵守 `can_use_for_writing` 与 `can_use_for_citation`。
- **网页审核包**：content review bundle v1 已废弃；v2 只接收 proposal，提供 history 与受保护 retention，不提供直接 apply 路径。
- **本地产物边界**：`local/`、`literature-ai/outputs/tmp/`、`literature-ai/outputs/exports/`、`test-results/`、`.pytest_cache/` 和临时 scratch 脚本不属于源码，不应作为正式提交内容。
- **启动与恢复**：核心容器带健康检查；后端与 worker 只在 PostgreSQL、Redis、MinIO、GROBID 就绪后启动。数据库初始化使用 PostgreSQL advisory lock（咨询锁）串行化，失败可重试。
- **测试边界**：后端数据库测试使用独立随机 schema；前端 Playwright 使用 `4173`，不会复用 Owner 网关 `8000`。

## 生产环境（已部署）

- 生产服务器：`192.168.110.229`（Rocky 9.4，Docker Compose 9 服务），部署目录 `/opt/literature-ai`，更新源 `/opt/ai-shujvku-src`。
- 对外网站：`https://dft.researchlife.top`（cloudflared 隧道 → 本机 8000 Owner 网关；8080 为只读分享网关）。
- 数据真源：服务器 PostgreSQL `literature_ai` 库；PDF/文献库配置/解析模型在 `/opt/literature-ai/data/`。
- 备份、SSH 凭据、一键更新/运维命令、MCP key 等**敏感细节不在本 README**，统一见根 [`AGENTS.md`](./AGENTS.md) 与本机 `local/srv_deploy/README.md`（`local/` 已 gitignore，不进仓库）。
- 改代码上线链路：本机 push GitHub → 服务器 `/opt/ai-shujvku-src/update.sh`。
- ⚠️ 已知安全待加固项与权限分层规则见 [`AGENTS.md`](./AGENTS.md)。

## 快速启动

```bash
cd literature-ai
cp .env.example .env
# 把 .env 中的占位 secret 改成真实值后再启动
docker compose up --build
curl http://localhost:8000/api/health
```

主工作台：<http://localhost:8000/pages/literature_library/index.html>

## 验证入口

在仓库根目录运行：

```bash
python scripts/verify.py fast
python scripts/verify.py full
```

`fast` 用于日常改动，包含仓库结构、Python 编译、关键后端回归和前端重点流程；`full` 在此基础上运行全部 pytest 与 Playwright。两者都不会连接或改写真实业务 schema。

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
| [literature-ai/docs/ARCHITECTURE.md](./literature-ai/docs/ARCHITECTURE.md) | 当前架构、模块边界、启动与测试边界 |
| [literature-ai/docs/CONTENT_REVIEW_WORKFLOW.md](./literature-ai/docs/CONTENT_REVIEW_WORKFLOW.md) | Content Knowledge、review bundle v2、AI Writer 与写作/引用资格边界 |
| [literature-ai/README.md](./literature-ai/README.md) | `literature-ai/` 目录落点说明；不再承载完整主说明 |

如果这些文档出现冲突，以本文件、`literature-ai/AGENTS.md`、当前代码行为和测试结果为准。

## 运行与提交边界

- 不要提交本地 token、数据库连接串、临时探针脚本或本地调试输出。
- 根目录下的 `local/` 与 `literature-ai/outputs/tmp/`、`literature-ai/outputs/exports/`、`test-results/`、`.pytest_cache/`、`backend/reports/*backup*/` 默认按“可清理或本地保留产物”处理。
- 如 IDE 会话缺少 MCP 工具，优先走仓库内受控后备路径，不要绕过权限边界直接操作 service、session、model 或数据库。

## 给新协作者的提醒

1. 先读 [literature-ai/AGENTS.md](./literature-ai/AGENTS.md)。
2. 以 `git status`、`git log` 和当前代码/测试结果为准，不要依赖旧计划文档猜测现状。
3. PostgreSQL 是唯一真源；如文档与代码冲突，优先修正文档，不要编造“已经完成”的迁移结论。
