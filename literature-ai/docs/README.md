# Literature AI 文档索引

本目录只作为当前文档入口。历史计划和审计可以保留，但如果它们与仓库主 README、AGENTS 或代码行为冲突，以仓库主 README、AGENTS 和测试结果为准。

## 当前有效入口

- [../../README.md](../../README.md): 仓库主 README，也是唯一项目入口。
- [../AGENTS.md](../AGENTS.md): AI 协作者规则、数据安全边界和临时产物规则。
- [../README.md](../README.md): `literature-ai/` 目录落点说明与入口跳转。
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

## 2026-06-28 后端维护基线

- MCP `import_analysis` 与 HTTP external-analysis import 共享 `ExternalAnalysisService.apply_review_rules_for_run(...)`，旧的 MCP 直连 `VerificationSessionService.apply_import_rules_for_paper(...)` 断言已更新为共享 service 边界断言。
- 已完成的后端/前端拆分包括 `review_conflict_service.py` 的 DFT helper、opinion collection、target summary/cache、active settlement/collapse/conflict-type helper 拆分，以及 `paper_workbench_service.py` 的 review-center helper、workspace/source-document/audit/figure helper、AI reading package/content coverage/DFT evidence payload helper、PDF quality helper 拆分；当前仍应把大型 orchestration service 视为后续小步拆分对象。
- 本轮仅改代码、测试和文档；未修改真实 `data/`、`artifacts/`、registry，也未执行 extraction apply。
- 最近验证通过：`python -m compileall -q app/services`、`tests/test_mcp_new_tools.py`、MCP/import-analysis 相关 `test_mcp_server.py` 子集、external-analysis import/apply 子集、`tests/test_review_adjudication_service.py`、`tests/test_dft_conflict_settlement.py`、`tests/test_codex_workbench_v1.py`、`tests/test_pdf_pipeline_hardening.py -k quality`、`tests/test_storage_root_resolution.py`、以及 `tests/test_papers_api.py` 的 review-center/workspace/supplementary 子集。

## 历史与计划目录

- `plans/`: 计划和路线图，有些内容是历史阶段记录。
- `audits/`: 审计和验收记录，有些内容描述当时的状态，不代表当前代码。
- `walkthrough.md`: 历史阶段汇报，保留作追溯，不作为最新基线。

需要更新项目说明时，优先同步 `../../README.md`、`../AGENTS.md`、本文件；如 `literature-ai/` 目录入口变化，再同步 `../README.md` 的落点说明。
