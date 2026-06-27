# Literature AI 文档索引

本目录只作为当前文档入口。历史计划和审计可以保留，但如果它们与当前 README、AGENTS 或代码行为冲突，以当前 README、AGENTS 和测试结果为准。

## 当前有效入口

- [../README.md](../README.md): 子项目当前定位、启动方式和验证闸门。
- [../AGENTS.md](../AGENTS.md): AI 协作者规则、数据安全边界和临时产物规则。
- [../使用说明.md](../%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md): 日常使用入口和常见问题。
- [mcp/MCP_API.md](mcp/MCP_API.md): MCP API 与工具说明。
- [schema/dft_ml_dataset_schema.md](schema/dft_ml_dataset_schema.md): DFT ML dataset 导出契约。
- [schemas/dft_results_ml_v1.md](schemas/dft_results_ml_v1.md): DFT results 相关 schema 说明。

## 当前稳定边界

- PostgreSQL + pgvector 是唯一业务数据源。
- DFT 抽取结果默认是候选，必须经过证据、审核、材料绑定和导出安全门。
- Literature Library 的 DFT 页按催化剂样本分组，但保留每条 DFT 记录的审核、证据和操作入口。
- Catalyst sample 的身份可由 DFT 行提供，基础信息可由 catalyst extractor 或前端补全合流。
- `potential_determining_step` 是表格上下文，不作为无数值 DFTResult 候选入库。
- `outputs/tmp/`、`outputs/exports/`、`test-results/`、`.pytest_cache/` 和 scratch 脚本默认不提交。

## 历史与计划目录

- `plans/`: 计划和路线图，有些内容是历史阶段记录。
- `audits/`: 审计和验收记录，有些内容描述当时的状态，不代表当前代码。
- `walkthrough.md`: 历史阶段汇报，保留作追溯，不作为最新基线。

需要更新项目说明时，优先同步本文件、根 README、子项目 README、`使用说明.md` 和 `AGENTS.md`。
