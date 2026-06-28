# 局域网多 AI 并发核验执行方案

本文档描述多台电脑上的外部 AI 如何通过本机 MCP/API 共同核验同一套文献数据库。

核心原则：

- 本机是主机，运行后端、PostgreSQL、Redis、worker、文件存储。
- 其它电脑只通过局域网访问本机 MCP/API。
- 其它电脑不直接修改本机共享文件夹、数据库文件、JSON 或图片文件。
- DFT 数据走“双 AI 候选审核 + 共识/仲裁”。
- 非 DFT 模块允许 AI 直接修正，但必须先获取模块写入锁。
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

普通外部 AI 使用安全能力集：

```text
read_papers,append_notes,propose_corrections,request_parse
```

不要给普通外部 AI `review_corrections`，除非它被明确指定为可信管理员。
不要给普通外部 AI、DFT 审核 AI 或 propose-only key `repair_dft_issues`。DFT audit issue 修复使用单独的主修复 key：

```text
dft_primary_repair|DFT Primary Repair AI|<key>|read_papers,repair_dft_issues
```

审核 AI 可使用 `read_papers,propose_corrections` 创建候选、issue 或审核意见；主修复 AI 才能调用 `repair_dft_audit_issue`，且修复结果仍不是人工确认或 ML_Ready。
修改 `LITAI_MCP_API_KEYS` 后，检查 `/api/system/agent-guide` 的 `mcp.capability_warnings` 或 `/api/settings/ide-prompts` 的 `mcp_capability_warnings`。如果出现 `repair_dft_issues_non_primary_repair_key`，说明 repair capability 被配到了非主修复 key；warning 只包含 source/display/capability，不包含 raw key。

建议给不同电脑或不同 AI 使用不同 `source_prefix`，例如：

```text
ai_pc_1|AI PC 1|<key>|read_papers,append_notes,propose_corrections,request_parse
ai_pc_2|AI PC 2|<key>|read_papers,append_notes,propose_corrections,request_parse
dft_primary_repair|DFT Primary Repair AI|<key>|read_papers,repair_dft_issues
```

这样 `audit_logs`、`external_analysis_runs`、`workflow_jobs` 可以区分是谁做的。

## 3. 推荐分工

同一篇文献可以并行，但不要让多个 AI 同时直接写同一个模块。

推荐分配：

```text
AI-1：DFT 数据核验 + 图片/截图核验
AI-2：章节核验 + 写作模块核验
AI-3：另一篇文献的章节/写作模块，或 DFT 第二意见
```

更细的模块名：

```text
sections        章节
writing_cards   写作模块
figures         图片、截图、裁图、figure 元数据
tables          表格
content         元数据 + 章节 + 写作模块 + notes
all_non_dft     所有非 DFT 直接写入模块
```

## 4. DFT 数据规则

DFT 是高风险数据，不需要模块写入锁来提交候选意见，因为两个 AI 可以同时审核同一条 DFT 数据。

DFT 推荐流程：

1. `query_papers` 找到文章。
2. `get_codex_context` 读取整体上下文。
3. `get_dft_review_queue` 找到待审核 DFT 候选。
4. `get_codex_item(item_type="dft_result")` 读取单条 DFT 候选。
5. `read_paper_page` 核对 PDF 原文页。
6. `import_analysis(raw_payload.object_review_audits)` 提交 AI 审核意见。

补充：

- 对任何文献，如果 AI 发现 parser 漏提的 DFT 行，并且希望它稳定进入系统候选队列，必须提交 `decision="new_candidate"` 的结构化对象，并在 `import_analysis` 时使用 `auto_apply_review_rules=true`。
- 这一步会把漏项 materialize 成未验证 `DFTResult` candidate，供后续双 AI 复核、冲突裁决和导出门控继续处理。
- 这不是最终入库通过，也不会直接解锁导出。

规则：

- AI_A 和 AI_B 可以审核同一篇、同一条 DFT 数据。
- 两个 AI 意见一致时，系统可自动应用安全规则。
- 意见不一致时，进入冲突队列。
- 第三 AI 可以用 `adjudication_role="third_ai"` 提交仲裁候选。
- 普通 AI 不直接调用最终验证/入库工具。

## 5. 非 DFT 直接写入规则

章节、写作模块、图片、表格等非 DFT 模块可以由 AI 直接修正，但必须先拿模块写入锁。

MCP 流程：

```text
1. acquire_module_write_lock(paper_id, module_name, locked_by)
2. get_codex_context(paper_id)
3. get_codex_item(...) 或 read_paper_page(...)
4. import_analysis(..., auto_apply_review_rules=true, reviewer=locked_by, write_lock_token=token)
5. release_module_write_lock(lock_token)
```

HTTP API 流程：

```text
POST /api/module-locks/acquire
POST /api/external-analysis/import
POST /api/module-locks/release
```

如果没有 `write_lock_token`，`auto_apply_review_rules=true` 的非 DFT 直接写入会被拒绝。

如果只想提交候选意见、不直接改数据，可以设置：

```json
{
  "auto_apply_review_rules": false
}
```

这种候选导入不需要写入锁。

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

导入并直接应用非 DFT 修正：

```json
{
  "paper_id": "PAPER_UUID",
  "source": "assigned_content_audit",
  "source_label": "AI PC 2 content audit",
  "reviewer": "ai_pc_2",
  "auto_apply_review_rules": true,
  "write_lock_token": "TOKEN",
  "raw_payload": {
    "correction_proposals": [
      {
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
        }
      }
    ]
  }
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
开始前获取 content 模块写入锁。
所有工作必须留痕。
完成后释放写入锁。
```

图片任务：

```text
通过 MCP 和 API 核验 A0005 文章的图片、截图和 figure 元数据。
优先走 MCP 和 API；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。
开始前获取 figures 模块写入锁。
需要核对 PDF 原文页。
所有工作必须留痕。
完成后释放写入锁。
```

DFT 第二意见：

```text
通过 MCP 和 API 核验 A0005 文章的 DFT 数据。
优先走 MCP 和 API；如果当前 IDE 会话没有暴露 MCP 工具，可改用仓库内 `literature-ai/backend` 的 `app.mcp.*` 后备路径。
必须核对 PDF 原文证据。
只提交 object_review_audits 候选意见，不直接最终入库。
所有工作必须留痕。
```

## 8. 失败处理

常见失败：

- `module_write_lock_required`：缺少写入锁。先调用 `acquire_module_write_lock`。
- `module_write_lock_conflict`：同篇同模块已被其它 AI 占用。等待释放、换模块、或等锁过期。
- `module_write_lock_owner_mismatch`：锁归属和 `reviewer/locked_by` 不一致。使用同一个 AI 身份。
- `artifact_precondition_failed`：PDF、Markdown、Docling JSON 或 `ai_reading_package` 不完整。先修复解析产物。

如果 AI 卡住，不要直接改文件夹。让它提交 note 或候选意见，等待人工或主机端处理。
