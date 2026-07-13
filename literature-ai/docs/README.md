# Literature AI 文档索引

本目录只作为当前文档入口。历史计划和审计可以保留，但如果它们与仓库主 README、AGENTS 或代码行为冲突，以仓库主 README、AGENTS 和测试结果为准。

## 当前有效入口

- [../../README.md](../../README.md): 仓库主 README，也是唯一项目入口。
- [../AGENTS.md](../AGENTS.md): AI 协作者规则、数据安全边界和临时产物规则。
- [../README.md](../README.md): `literature-ai/` 目录落点说明与入口跳转。
- [SERVER_GITHUB_WORKFLOW.md](SERVER_GITHUB_WORKFLOW.md): 服务器优先改代码、SSH 推 GitHub、服务重启与验收步骤。
- [mcp/MCP_API.md](mcp/MCP_API.md): MCP API 与工具说明。
- [ARCHITECTURE.md](ARCHITECTURE.md): 当前架构、模块职责、健康检查和测试边界。
- [schema/dft_ml_dataset_schema.md](schema/dft_ml_dataset_schema.md): DFT ML dataset 导出契约。
- [schemas/dft_results_ml_v1.md](schemas/dft_results_ml_v1.md): DFT results 相关 schema 说明。
- [plans/offline_web_ai_dft_review_bundle.md](plans/offline_web_ai_dft_review_bundle.md): 离线网页 AI DFT 核验包、返回校验和受控导入方案。

## 当前稳定边界

- PostgreSQL + pgvector 是唯一业务数据源。
- DFT 抽取结果默认是候选，必须经过证据、审核、材料绑定和导出安全门。
- Literature Library 的 DFT 页按催化剂样本分组，但保留每条 DFT 记录的审核、证据和操作入口。
- Catalyst sample 的身份可由 DFT 行提供，基础信息可由 catalyst extractor 或前端补全合流。
- `potential_determining_step` 是表格上下文，不作为无数值 DFTResult 候选入库。
- `outputs/tmp/`、`outputs/exports/`、`test-results/`、`.pytest_cache/` 和 scratch 脚本默认不提交。

## 2026-07-13 维护基线

- 同步 PDF 导入失败会先回滚数据库会话，再按原始错误更新 workflow job；路径导入、上传和附加 PDF 均有真实 PostgreSQL 回归测试。
- 数据库初始化按 URL 成功后才缓存，并通过 advisory lock 防止多进程并发执行迁移；必需步骤失败会显式报错且允许重试。
- Review Center 的 CSS/JS 已从 6000 多行 HTML 拆出；人工复核进度、bundle 来源文献和图像压缩逻辑已有共享模块。
- Docker Compose 有核心服务健康检查和依赖门；Playwright 固定使用隔离端口 `4173`。
- 统一验证入口为仓库根目录的 `python scripts/verify.py fast|full`。历史审计中的单次测试数字只代表当时快照，不应复制为当前结论。

## 历史与计划目录

- `plans/`: 计划和路线图，有些内容是历史阶段记录。
- `audits/`: 审计和验收记录，有些内容描述当时的状态，不代表当前代码。
- `walkthrough.md`: 历史阶段汇报，保留作追溯，不作为最新基线。

需要更新项目说明时，优先同步 `../../README.md`、`../AGENTS.md`、本文件；如 `literature-ai/` 目录入口变化，再同步 `../README.md` 的落点说明。
