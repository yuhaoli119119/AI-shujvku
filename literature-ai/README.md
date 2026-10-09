# AI-shujvku

个人科研仓库，唯一活跃系统是 `literature-ai`（本地文献 AI 系统）。数据真源只有一个：服务器 PostgreSQL `literature_ai` 库。

> **状态（2026-09-22）：目标流程核心链路已实现并上线。**
> 文献库、图表资料、数据表、汇总分析四个主页面已接入真实 API；
> 真实论文 A0019 已完成图表、取数、来源回看与公网浏览器验收。
> 独立复核和完整交付审计仍在进行，未完成前不得声称“全项目已闭环”。
> 详见 [`literature-ai/docs/PIPELINE_TARGET.md`](literature-ai/docs/PIPELINE_TARGET.md)。

## 一、运行环境与两个目录

| 项目 | 值（2026-09-22 实测） |
|---|---|
| 服务器 | `192.168.110.229`（Rocky Linux 9.4，Docker Compose） |
| 运行目录（真源） | `/opt/literature-ai` → 软链 `/opt/AI-shujvku/literature-ai`，**不是 git 仓库** |
| 源码工作区 | `/opt/ai-shujvku-src`（git 工作树） |
| 生产入口 | `https://dft.researchlife.top`（登录见 `literature-ai/docs/auth/WORKBENCH_LOGIN.md`） |
| 数据库备份 | `/home/2401liyuhao/backups/literature-ai/database/` |

**两个目录可能不一致，不得假设"同一版本、已部署"。** 需要判断版本时，实测
`git -C /opt/ai-shujvku-src rev-parse HEAD`、`git -C /opt/ai-shujvku-src status`，
并与 `/opt/literature-ai` 里的实际文件、运行态对比后**如实报告差异**。

## 二、当前在做的事（目标流程，核心链路已上线）

先把**一篇论文**的完整数据闭环跑通，顺序固定：

1. 上传正文 PDF + SI：只保存关联（计划退出旧自动解析链）。
2. AI 整理图表：裁图、合并跨页表、逐个解释子图，保留页码/图号/图注/坐标/单位/材料关系与正文上下文。
3. AI 按反应模板（SRR / HER / OER / ORR / CO2RR）填实验与 DFT 数据。
4. 汇总分析：按需筛字段，不以"ML 就绪"作为入库门槛。

细节见 [`literature-ai/docs/PIPELINE_TARGET.md`](literature-ai/docs/PIPELINE_TARGET.md) 与
[`literature-ai/docs/DATA_RULES.md`](literature-ai/docs/DATA_RULES.md)。

## 三、先读什么

| 文档 | 作用 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 协作规则、角色分工（总指挥派发 / Codex-web 执行）、安全与删除边界 —— **先读这个** |
| [`SERVER_ACCESS.md`](SERVER_ACCESS.md) | 服务器接入事实（哪些能在服务器侧核验、哪些只是客户端别名） |
| [`MCP_单篇论文执行指令.md`](MCP_单篇论文执行指令.md) | 单篇论文执行指令（目标流程，计划中） |
| [`literature-ai/docs/README.md`](literature-ai/docs/README.md) | 项目文档索引（现行 / 历史） |
| [`literature-ai/docs/CODEX_WEB_DISPATCH.md`](literature-ai/docs/CODEX_WEB_DISPATCH.md) | 总指挥 → Codex-web 派发流程与脚本用法 |

## 四、派发与执行

- **总指挥**（外部，额度有限）：分派任务、验收、决定 goal 何时置 `complete`。
- **Codex-web**（服务器上，本仓库内）：只执行本轮被派发的任务。派发方式：
  `scripts/codex_web_dispatch.cjs`（用法见 `literature-ai/docs/CODEX_WEB_DISPATCH.md`）。
- 一个任务只在一个 Codex-web 线程里做：**不分叉、不加载其它会话、不创建子任务**；
  派发后由总指挥侧的回查机制（如每 10 分钟 heartbeat）跟踪，脚本自身不负责唤醒任何人。

## 五、数据资产与不可动清单

数据库、`/opt/literature-ai/data/storage` 下的 PDF/SI、`data/libraries`、docling 缓存、`outputs/`、`deliverables/`、
备份与凭据一律保留。删除任何东西前必须逐文件列名并取得用户确认（细则见 `AGENTS.md`）。
