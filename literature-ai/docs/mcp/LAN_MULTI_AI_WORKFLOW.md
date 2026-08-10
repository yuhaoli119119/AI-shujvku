# 局域网多客户端单 AI 验收执行方案

本文档描述多台电脑上的客户端如何通过本机 MCP/API 处理不同任务；每个内容候选的最终自动验收只由一个专用 AI 验收身份完成。

核心原则：

- 本机是主机，运行后端、PostgreSQL、Redis、worker、文件存储。
- 其它电脑只通过局域网访问本机 MCP/API。
- 其它电脑不直接修改本机共享文件夹、数据库文件、JSON 或图片文件。
- DFT 与非 DFT 内容统一走“单一 AI 自动验收，人工只处理异常”。
- 禁止对同一候选安排第二 AI、模型投票、AI 共识或第三 AI 仲裁。
- 普通非 DFT `import_analysis` 只保留候选/审核意见。非受信任的 `propose_correction` 直接写入中，服务端只对顶层 `abstract` 及结构化 `sections`、`mechanism_claims`、`writing_cards` 强制模块锁；表格、图像遵守专用工具的 capability/evidence 契约。
- 所有操作应优先通过 MCP/API，并在数据库中留下审计日志；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.context.mcp_auth_context` + `app.mcp.server` 后备路径，再将结果通过 MCP/API 风格的候选/审计写回。

## 1. 主机职责

主机负责：

- 保存 PDF、图片、Markdown、Docling JSON、工作区文件。
- 运行 `http://<主机局域网IP>:8000`。
- 提供 MCP 地址 `http://<主机局域网IP>:8000/mcp`。
- 统一写入 PostgreSQL。
- 执行裁图、PDF 渲染、数据库修正等受控操作。

其它电脑可以读取上下文、提交审核意见、请求受控写入，但不应该直接写主机文件夹。

## 2. 外部 AI 权限建议

普通候选生产/修复 AI 使用安全能力集：

```text
read_papers,append_notes,propose_corrections,request_parse
```

不要给普通外部 AI `review_corrections`，除非它被明确指定为可信管理员。
不要给普通外部 AI、DFT 审核 AI 或 propose-only key `repair_dft_issues`。DFT audit issue 修复使用单独的主修复 key：

```text
dft_primary_repair|DFT Primary Repair AI|<key>|read_papers,repair_dft_issues
```

普通 AI 可使用 `read_papers,propose_corrections` 创建候选、issue 或审核意见；主修复 AI 才能调用 `repair_dft_audit_issue`，且修复结果仍不是已确认或 ML_Ready。专用验收身份使用 `read_papers,ai_verify_content`，不得与普通候选身份混用。
修改 `LITAI_MCP_API_KEYS` 后，检查 `/api/system/agent-guide` 的 `mcp.capability_warnings` 或 `/api/settings/ide-prompts` 的 `mcp_capability_warnings`。如果出现 `repair_dft_issues_non_primary_repair_key`，说明 repair capability 被配到了非主修复 key；warning 只包含 source/display/capability，不包含 raw key。

建议给不同电脑或不同 AI 使用不同 `source_prefix`，例如：

```text
ai_pc_1|AI PC 1|<key>|read_papers,append_notes,propose_corrections,request_parse
ai_pc_2|AI PC 2|<key>|read_papers,append_notes,propose_corrections,request_parse
dft_primary_repair|DFT Primary Repair AI|<key>|read_papers,repair_dft_issues
single_verifier|Single AI Verifier|<key>|read_papers,ai_verify_content
```

这样 `audit_logs`、`external_analysis_runs`、`workflow_jobs` 可以区分是谁做的。

## 3. 推荐分工

可以并行处理互不重叠的文章或目标，但禁止多个 AI 同时验收同一个候选。

推荐分配：

```text
验收 AI：负责分配范围内全部候选的唯一验收决定
修复 AI：只修复候选，不能授予 ai_verified
其它客户端：处理另一篇文献或互不重叠的模块
```

可用于强制锁或额外操作协调的模块名：

```text
sections        章节
writing_cards   写作模块
figures         图片、截图、裁图、figure 元数据
tables          表格
content         元数据 + 章节 + 写作模块 + notes
all_non_dft     可选的跨非 DFT 协调范围
```

这些 scope 的存在不表示每个字段都由服务端强制锁。`ReviewService` 对非受信任直接写入的强制范围仅为顶层 `abstract`，以及 `sections`、`mechanism_claims`、`writing_cards`；`title`、`year`、`journal`、`authors` 等其他允许字段可按操作需要额外加锁，但不能描述为必然强制锁。

## 4. DFT 数据规则

DFT 是高风险数据。候选生产可以不使用模块写入锁，但同一条 DFT 候选只能由一个专用验收 AI 提交最终自动验收结果。

DFT 推荐流程：

1. `query_papers` 找到文章。
2. `get_codex_context` 读取整体上下文。
3. `get_dft_review_queue` 找到待审核 DFT 候选。
4. `get_codex_item(item_type="dft_result")` 读取单条 DFT 候选。
5. `read_paper_page` 核对 PDF 原文页。
6. 普通 AI 可用 `import_analysis(raw_payload.object_review_audits)` 提交候选意见；专用验收 AI 使用 `submit_ai_verification_batch` 完成验收。

补充：

- 对任何文献，如果 AI 发现 parser 漏提的 DFT 行，并且希望它稳定进入系统候选队列，必须提交 `decision="new_candidate"` 的结构化对象，并在 `import_analysis` 时使用 `auto_apply_review_rules=true`。
- 这一步会把漏项 materialize 成未验证 `DFTResult` candidate，供后续单一 AI 验收和导出门控继续处理。
- 这不是最终入库通过，也不会直接解锁导出。

规则：

- 同一候选只允许一个具有 `ai_verify_content` 的已认证验收身份处理。
- 验收 AI 必须重新读取真实 PDF、核对精确页码/定位、目标版本和 DFT 值/单位/材料绑定。
- 可安全修正的项目自动修正后通过；证据不符自动拒绝；条件不足或冲突未解进入 `exception`。
- 普通 AI 不直接调用最终验收工具，人工只处理 `exception` 队列。

## 5. 非 DFT 候选与真实修改入口

`import_analysis` 用于导入普通候选和审核意见。当前非 DFT 分支会保留对象并标记 `authenticated_human_review_required` / `no_ai_overwrite`；即使传入 `auto_apply_review_rules=true` 和锁，也不会执行后写覆盖。

真实修改入口按对象类型区分：

- `propose_correction`：对允许的元数据、章节、写作卡、机理声明等执行受控修改；非受信任直接应用顶层 `abstract` 或结构化 `sections`、`mechanism_claims`、`writing_cards` 时必须使用相应模块写入锁，其他允许字段不由服务端普遍强制。
- `update_table` / `create_table` / `merge_table` / `delete_table`：表格对象直接工具，使用其专用 capability 与结构化证据。
- `review_figure` / `recrop_figure` / `create_figure_from_bbox`：图像审核、重裁和补图直接工具；不得把 bbox/裁图请求伪装成 `import_analysis`。
- `import_analysis`：只记录候选/意见；DFT `new_candidate` 是受控物化为未验证候选的例外，但不是最终验收。

服务端强制锁字段或主动采用额外协调锁时的 `propose_correction` 流程：

```text
1. acquire_module_write_lock(paper_id, module_name, locked_by)
2. get_codex_context(paper_id)
3. get_codex_item(...) 或 read_paper_page(...)
4. propose_correction(..., write_lock_token=token)
5. 回读目标对象
6. release_module_write_lock(lock_token)
```

只提交候选意见时直接调用 `import_analysis`，不需要申请用于真实修改的模块锁。

## 6. 写入锁示例

获取写入锁：

```json
{
  "paper_id": "PAPER_UUID",
  "module_name": "content",
  "locked_by": "ai_pc_2",
  "ttl_minutes": 30,
  "metadata": {
    "task": "核验章节和写作模块"
  }
}
```

返回中会包含：

```json
{
  "lock_token": "TOKEN"
}
```

使用锁执行受控非 DFT 修正：

```json
{
  "paper_id": "PAPER_UUID",
  "field_name": "sections",
  "target_path": "sections:new:create",
  "operation": "create",
  "proposed_value": {
    "section_title": "Results",
    "section_type": "results",
    "text": "Recovered section text.",
    "page_start": 3,
    "page_end": 4
  },
  "reason": "The parser missed this section.",
  "evidence_payload": {
    "page": 3,
    "quoted_text": "Recovered section text."
  },
  "write_lock_token": "TOKEN"
}
```

释放写入锁：

```json
{
  "lock_token": "TOKEN",
  "released_by": "ai_pc_2"
}
```

## 7. 简单提示词模板

你可以对外部 AI 只说：

```text
通过 MCP 和 API 核验 A0005 文章的章节和写作模块。
优先走 MCP 和 API；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。
不允许直接修改文件夹。
import_analysis 只导入候选/审核意见；需要实际修改章节或写作卡时，获取 content 模块写入锁并调用 propose_correction。
所有工作必须留痕并回读；完成后释放写入锁。
```

图片任务：

```text
通过 MCP 和 API 核验 A0005 文章的图片、截图和 figure 元数据。
优先走 MCP 和 API；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。
import_analysis 只记录候选意见；figure 元数据受控修改使用 figures 锁和 propose_correction，图像重裁/补图使用专用直接工具。
需要核对 PDF 原文页。
所有工作必须留痕。
完成后释放写入锁。
```

DFT 单一 AI 验收：

```text
通过 MCP 和 API 核验 A0005 文章的 DFT 数据。
优先走 MCP 和 API；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。
必须核对 PDF 原文证据。
使用专用 `ai_verify_content` 身份调用验收工具；不得增加第二 AI 或投票流程。
证据充分则自动通过或安全修正后通过；异常才进入人工队列。
所有工作必须留痕。
```

## 8. 失败处理

常见失败：

- `module_write_lock_required`：缺少写入锁。先调用 `acquire_module_write_lock`。
- `module_write_lock_conflict`：同篇同模块已被其它 AI 占用。等待释放、换模块、或等锁过期。
- `module_write_lock_owner_mismatch`：锁归属和 `reviewer/locked_by` 不一致。使用同一个 AI 身份。
- `artifact_precondition_failed`：PDF、Markdown、Docling JSON 或 `ai_reading_package` 不完整。先修复解析产物。

如果 AI 卡住，不要直接改文件夹。让它提交 note 或候选意见，等待人工或主机端处理。
