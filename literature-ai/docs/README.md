# 文档索引

## 当前有效文档

- [current_baseline.md](./current_baseline.md)：当前架构基线，优先级高于历史审计/计划记录
- [../AGENTS.md](../AGENTS.md)：AI 协作者规则与交付边界
- [../README.md](../README.md)：子项目技术说明
- [../使用说明.md](../%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md)：中文使用说明
- [mcp/MCP_API.md](./mcp/MCP_API.md)：当前 MCP 接口使用说明
- [mcp/AI_TASK_ROUTING.md](./mcp/AI_TASK_ROUTING.md)：自然语言 AI 任务到 MCP 工具的动态路由说明
- [mcp/MCP_IMPLEMENTATION.md](./mcp/MCP_IMPLEMENTATION.md)：当前 MCP 实现与安全边界
- [plans/litai_extraction_review_protocol_v0_1.md](./plans/litai_extraction_review_protocol_v0_1.md)：LitAI 当前提取与审核协议草案
- [plans/future_backlog_dynamic_ai_workbench.md](./plans/future_backlog_dynamic_ai_workbench.md)：动态 AI 文献工作台后续改进 backlog

## 近期阶段性完成记录

- [audits/D6_dynamic_ai_mcp_review_workflow_closure.md](./audits/D6_dynamic_ai_mcp_review_workflow_closure.md)：动态 AI/MCP 审核工作流阶段性 closure，覆盖任务路由、DFT evidence queue、外部审核覆盖、冲突聚合、对象级 review payload 与剩余 backlog

## 历史/参考计划

- [plans/codex_centered_refocus.md](./plans/codex_centered_refocus.md)：历史 Codex 中心化改造方向；其中固定 Codex 角色表述已被当前动态 AI 分工基线取代
- [plans/codex_centered_execution_plan.md](./plans/codex_centered_execution_plan.md)：历史 Codex 中心化执行目标；保留为验收记录和工具命名背景

## 目录说明

- `docs/mcp/`：当前仍有效的 MCP 文档
- `docs/plans/`：历史/当前计划混合目录；与当前基线冲突时，以 `current_baseline.md` 为准
- `docs/audits/`：历史验收与审计记录，不代表当前架构基线

## 文档使用规则

- 提到 SQLite active DB、固定 Codex/Gemini 分工、无 bibliography generation、旧 D2/D3/D4 限制的文档均视为历史记录
- 当前真实状态请以根 README、`AGENTS.md`、`docs/current_baseline.md`、`使用说明.md` 和 git history 为准
