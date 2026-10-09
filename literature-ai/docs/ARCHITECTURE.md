# 现行系统结构（2026-09-23 实测）

> 本文只描述**当前实际运行**的系统。目标流程见 `PIPELINE_TARGET.md`，两者不要混读。

## 1. 运行位置

| 项目 | 值（2026-09-22 实测） |
|---|---|
| 服务器 | `192.168.110.229`（Rocky Linux 9.4） |
| 运行目录 | `/opt/literature-ai` → 软链 `/opt/AI-shujvku/literature-ai`（**不是 git 仓库**） |
| 源码工作区 | `/opt/ai-shujvku-src`（git 工作树） |
| 数据真源 | 服务器 PostgreSQL 库 `literature_ai` |
| 公网入口 | `https://dft.researchlife.top`（cloudflared 隧道 → 本机 Owner 网关） |

### 源码目录 vs 运行目录：可能不一致

- 运行目录没有 `.git`；源码工作区有自己的 git 历史与工作树状态。
- 2026-09-22 实测：源码工作区分支 `codex/figure-library-recovery-20260915-061500`，
  HEAD `e440e03e`，工作树有 3 个未提交修改文件 + 1 个未跟踪目录（git status 见仓库根 `AGENTS.md`）。
- 运行目录的对应文件**不保证**与源码工作区相同。要判断"服务器跑的是什么"，必须直接看运行目录里的文件与运行态，
  不能拿工作区文件代替；有差异要如实报告，不允许写成"两处完全相同、已部署"。
- 更新链路存在（`/opt/ai-shujvku-src/update.sh`：git pull → rsync 到运行目录 → 重建部分容器），
  但它会重建容器、造成短暂中断，只在确实需要且经用户同意时才执行；2026-09-22 本轮**未执行**。

## 2. 服务（Docker Compose，10 个）

2026-09-22 实测状态：

| 服务 | 状态 | 说明 |
|---|---|---|
| `backend` | healthy | FastAPI 后端；挂载 `./backend:/app`、`./frontend:/frontend`、`./prompts:/prompts:ro` |
| `worker` | up | 异步任务 |
| `worker-pdf` | up | PDF 解析任务 |
| `postgres` | healthy | 业务数据真源 |
| `redis` | healthy | 队列/缓存/会话吊销名单 |
| `minio` | healthy | 对象存储 |
| `grobid` | healthy | PDF 结构解析 |
| `owner-gateway` | up | 对内主网关（会话鉴权） |
| `public-gateway` | up | 公网入口 |
| `share-gateway` | up | 只读分享网关 |

改后端 Python 后需要重启对应容器；改 `frontend/` 下的静态页面即时生效（不重建容器）。

## 3. 数据资产（一律保留）

| 内容 | 位置 | 实测 |
|---|---|---|
| 数据库（真源） | PostgreSQL `literature_ai` | 99 篇论文 |
| 文献库配置 | `/opt/literature-ai/data/libraries` + `data/library_registry.json` | 4 个库：默认文献库 / 石墨炔 / 双原子催化剂 / 锂硫双原子（激活：锂硫双原子） |
| PDF 原文（含 SI） | `/opt/literature-ai/data/storage` | 约 1.9G |
| docling 解析模型 | `/opt/literature-ai/data/docling_cache` | 约 506M |
| 数据库备份 | `/home/2401liyuhao/backups/literature-ai/`（`database/` 下为 pg_dump） | 按需新增，不覆盖 |

`outputs/`、`deliverables/`、`backend/reports/` 中的历史报告是**历史产物，不是当前规范**（见 `README.md`）。

## 4. 访问与鉴权

- 工作台登录（会话 Cookie + `auth_request` 网关）见 [`auth/WORKBENCH_LOGIN.md`](auth/WORKBENCH_LOGIN.md)，
  2026-09-22 复核：页面未登录 → `302 /login`；`/api/*` → `401`；`/login`、`/api/health`、`/.well-known/*` → `200`；
  `/docs`、`/redoc`、`/openapi.json`、外部 `/api/auth/verify` → `404`；`/mcp` 不带 key → `401`。
- MCP 接入见 [`MCP_ACCESS.md`](MCP_ACCESS.md)。

## 5. 脚本

- 仓库级脚本在源码工作区 `/opt/ai-shujvku-src/scripts/`（含 `verify.py`、派发脚本 `codex_web_dispatch.cjs`）。
- 运行目录 `/opt/literature-ai/scripts/` 只放运行期需要的脚本（如 `litai_auth_user.py`），
  不含仓库验证脚本；两处脚本集合不同属正常现象，不要互相覆盖。
- 派发脚本在运行目录有同名副本，便于服务器内直接调用：
  `/opt/literature-ai/scripts/codex_web_dispatch.cjs`。

## 6. 其他已运行的本地服务（不属于 literature-ai）

- Codex-web（本机 `127.0.0.1:8214`；后端容器通过 `litai-codex-web-bridge.service` 桥接 `172.18.0.1:8214` 访问，派发通道见 `CODEX_WEB_DISPATCH.md`）。
- Agent Canvas / OpenHands 容器（Agent Canvas 的 `127.0.0.1:8000` 是它自己的 API，**不是** Literature AI 的 Owner 网关）。
- 这些服务与本项目共用一台机器，但**不要**把它们当成 literature-ai 的一部分去改。

## 7. 现状 vs 目标（2026-09-23 实测）

**现状（新流程已跑通，旧流程保留但入口隐藏）**
- 新上传：只保存 PDF/SI 关联，不启动旧自动解析链（`ingestion.py` 明确 `automatic_parsing_started=false`）。
- 一键 AI 提取：`POST /api/rebuild/papers/{paper_id}/ai-extract/jobs` 派发 Codex-web 目标模式线程；任务完成后自动把 `rebuild_visual_assets` 子图合成整图写回 `paper_figures`，旧详情页直接可见。
- 新流程四页已上线：AI 提取、AI 图表整理、AI 数据表、AI 汇总分析；主导航"更多"菜单列出。
- 旧审核链路代码（`review_center`、`dft-workflow`、`content_knowledge`、`verification`）仍保留，但导航入口已隐藏；旧自动解析不再对新上传触发。
- A0019 已验证：37 子图、58 数据行、75 数值、105 来源；6 张整图已写回旧详情页。

**目标**：文献库 → AI 整理图表 → AI 按反应模板填表 → 汇总分析（见 `PIPELINE_TARGET.md`，**核心链路已实现，独立验收进行中**）。
