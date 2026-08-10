# 离线网页 AI DFT 核验包执行方案

## 目标

在不向网页 AI 暴露 MCP、数据库、服务器工具或完整 PDF 的前提下，按单篇主文献手动导出一个小型 DFT 核验包。网页 AI 只返回结构化审核建议；本地执行 AI 先校验，再通过现有 `import_analysis` 受控入口写回候选和审核意见。

## 统一抽象

流程统一为：

```text
初步解析结果
  -> evidence package
  -> review proposal
  -> validation
  -> authenticated import_analysis
```

审核来源使用 `review_source_type`、`reviewer_label`、`reviewer_model` 和 `tool_capabilities` 描述，不把产品名或模型名写死。现有带有历史名称的兼容 API 可以继续保留，但不作为新协议的身份模型。

## 实施分层

### 1. 手动导出

- 文献详情页“更多操作”增加“导出 AI 核验包”。
- `POST /api/papers/{paper_id}/dft-review-bundle` 在内存中生成 ZIP 后直接返回。
- 不在数据库登记导出记录，不在服务器长期保存压缩包，不复制完整 PDF。
- 默认只放入 DFT 相关文字片段、表格、图片、现有 DFT candidate 和计算参数。
- 主文与已关联 SI 共用一个包；SI 只作为主文证据来源。

### 2. 可追溯返回协议

- 包内提供 `manifest.json`、`return_schema.json`、`return_template.json` 和 `instructions_for_web_ai.md`。
- 每个证据项有稳定的包内 `evidence_id`，例如 `main:text:001`、`si:table:001`。
- `bundle_fingerprint` 覆盖文献身份、当前候选和证据内容，用于发现错包或过期包。
- 已有候选只允许 `PASS`、`REVISE`、`REJECT`、`NEEDS_HUMAN`；漏项使用 `new_candidate`。

### 3. 本地校验，不自动入库

- `POST /api/papers/{paper_id}/dft-review-result/validate` 校验 schema、paper_id、paper_code、包指纹、目标字段、目标记录和 evidence_id。
- 校验接口只返回 `import_analysis_request`，不创建 run、不写候选、不修改 DFT 数据。
- 本地执行 AI 复核校验结果后，再通过带认证身份的 MCP/API `import_analysis` 执行。
- 网页 AI 返回始终只是 proposal/candidate；本地 `import_analysis` 只把它导入候选/审核意见链。即使 DFT `new_candidate` 被物化为未验证行，也不等于最终验收。
- 自动权威验收由专用 `ai_verify_content` 身份依次调用 `get_ai_verification_tasks` 与 `submit_ai_verification_batch`；确定性门禁无法解决的 exception 才进入 Owner-session 人工处理。普通 `PASS` / `REVISE` / `REJECT` 意见不能替代该路径。

## 数据库影响

本功能不新增表、不新增字段、不执行迁移。审核来源继续进入现有 `external_analysis_runs` / `external_analysis_candidates`，正式 DFT 数据继续受现有验证和导出规则保护。

## 验收条件

1. ZIP 中不存在完整 PDF，且只含当前论文及相关 SI 的 DFT 材料。
2. 导出响应为 `Cache-Control: no-store`，服务端不产生长期 ZIP 文件。
3. 返回模板能通过同一 Pydantic schema 校验。
4. 错 paper_code、错 paper_id、错 fingerprint、未知 evidence_id、未知 target_id 均被拒绝。
5. 校验成功只生成受控 `import_analysis_request`，不会直接写数据库。
6. 后端测试、前端静态测试和浏览器实际下载均通过后再部署。
