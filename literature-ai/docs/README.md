# Literature AI 文档索引（2026-09-22 清洗后）

本目录只放**现行**文档。历史计划、审计、旧接口手册已移出，见文末"历史与备份"。

## 现行文档

| 文档 | 作用 |
|---|---|
| [`PIPELINE_TARGET.md`](PIPELINE_TARGET.md) | 目标流程：文献库 → AI 整理图表 → AI 按反应模板填表 → 汇总分析（**核心链路已实现，独立验收进行中**） |
| [`DATA_RULES.md`](DATA_RULES.md) | 数据规则：数值精度、分行、单元格溯源、单篇去重、冲突局部处理、分析筛字段 |
| [`AI_WORKBENCH_API.md`](AI_WORKBENCH_API.md) | 外部 AI 批量读取/写回图表解释与来源化数据的接口与提示词 |
| [`REBUILD_EXECUTION_LOG.md`](REBUILD_EXECUTION_LOG.md) | 全项目改造执行日志：阶段、备份、实测进度与阻碍 |
| [`REBUILD_ACCEPTANCE.md`](REBUILD_ACCEPTANCE.md) | 全项目改造验收报告：实测能力、A0019 闭环、测试摘要与回滚 |
| [`BATCH_COORDINATION.md`](BATCH_COORDINATION.md) | 当前对话的固定清单、串行文献任务、持久恢复、结果核对与原生 heartbeat 操作；阶段二源码候选，未部署/未真实执行 |
| [`CODEX_WEB_DISPATCH.md`](CODEX_WEB_DISPATCH.md) | 总指挥 → Codex-web 派发流程（目标模式）、脚本用法、安全与并发约束 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 现行系统结构：运行位置、服务、数据资产、访问与鉴权 |
| [`MCP_ACCESS.md`](MCP_ACCESS.md) | MCP 外部接入现状（端点、鉴权、能力分层、key 位置） |
| [`auth/WORKBENCH_LOGIN.md`](auth/WORKBENCH_LOGIN.md) | 工作台真实登录（2026-09-21 上线）；**现行且保持不动** |

上游规则（先读）：`AGENTS.md`（协作规则、角色与派发、安全与删除边界）、`README.md`（仓库/目录入口）、
`SERVER_ACCESS.md`（服务器接入事实）。这三份在源码工作区 `/opt/ai-shujvku-src/` 与运行目录 `/opt/literature-ai/` 下同名存在；
本目录内另有 `../AGENTS.md` 与 `../README.md` 作为上一级入口。

## 当前状态（必须如实转述）

- 目标流程**核心链路已实现并上线**：文献库、AI 提取、图表资料、数据表、汇总分析已经通过服务器内与公网浏览器验收。
- 2026-09-23 最新进展：一键 AI 提取（目标模式）已跑通；任务完成后自动写回旧详情页图片与解读；A0019 有 37 子图、58 数据行、75 数值、105 来源。
- 2026-09-22 已完成：新流程数据模型/API/页面、真实 PDF 裁图、来源化数据导入、去重/冲突保留、真实配对回归与 CSV 导出。
- 旧审核链路代码保留但导航入口已隐藏；新上传不触发旧自动解析。
- 当前仍处于独立验收与完整交付审计阶段，**不得**声称"全项目已闭环"。

## 历史与备份（不是现行规范）

以下内容保留但**不作为当前约定**：

- `backend/reports/`、`deliverables/`、`outputs/` 中的历史报告与导出快照 → 历史产物，仅供追溯。
- 清洗前的整套旧文档（含 `plans/`、`audits/`、`mcp/`、`schema/`、`schemas/`、`ui/` 共 51 个文件）已随
  `docs-cleanup-<UTCtimestamp>` 备份移出，备份位置见仓库根 `AGENTS.md` 的"文档清洗备份"一节；
  需要旧工具手册/审计记录时从备份读取，不要把它们当成现行规范。
- 旧的多层阻塞式审核规则、以及"在本机改动再上传 / 先拉到本机 diff"的流程说明，**已不再是现行规范**。
