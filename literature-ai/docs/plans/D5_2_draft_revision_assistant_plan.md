# D5-2 Draft Revision Assistant Plan

## 目标与非目标 (Goals and Non-Goals)
- **目标**:
  - 为用户提供段落级别的润色与改写建议 (Draft Revision Suggestions)。
  - 能够识别用户原文中缺乏证据支持的论点 (Unsupported Claims)，并主动打上 `unsupported_claim_needs_evidence` 的警告标签。
  - 对于带有引用建议的润色，保留候选文献的状态展示 (Evidence Status / Warnings)。
  - 强制标注所有的改写建议均为 `draft/suggestion-only`。
- **非目标**:
  - 不提供自动一键覆盖 (Auto-apply / auto-rewrite) 用户的原始论文。
  - 不生成最终引用或参考文献列表 (Bibliography)。
  - 不向数据库写入数据 (writes_db=false)。
  - 不改变任何现有文献记录或系统级别的 Verification 状态。

## 输入与输出 (Inputs and Outputs)
- **输入**:
  - `draft_text` (String): 用户需要润色或检查的原始草稿段落。
- **输出**:
  - `revision_suggestions` (List): 包含建议改写的内容。
    - `suggestion_type` (String): 比如 "grammar_fix", "clarity_improvement", "unsupported_claim"。
    - `original_excerpt` (String): 涉及的原文摘录。
    - `suggested_revision` (String): 建议的改写后文本。
    - `warnings` (List): 包含相关的安全与事实警告。
    - `candidate_papers` (List): 如果涉及特定证据补充，展示支持文献及其状态。
  - `safety_guardrails` (Object): 包含 `writes_db`, `auto_apply` 等全为 false 的标志位。

## 安全与防护标志 (Guardrails)
1. **Draft-only 标志**: 所有建议必须通过 `warnings` 包含 `draft_do_not_use_as_final_fact`，明确其为非事实最终版。
2. **Unsupported Claims 处理**: 当识别到文中提出的结论缺乏支撑时，即使返回建议改写，也必须挂载 `unsupported_claim_needs_evidence`，提醒用户补全文献。
3. **安全拦截器 (Safety Guardrails 结构)**:
   - `writes_db: false`
   - `auto_apply: false`
   - `generates_bibliography: false`
   - `export_unlocked: false`
   - `verified_status_changed: false`

## 证据候选展示 (Candidates & Evidence Status)
- 复用 `WritingCitationCandidateService` 能够给改写建议提供后备的文献支持。如果改写牵涉到的文献只是 `metadata_only` 或是 `pending_review`，这些文献不可转正，警告信息必须向上透传，标注 `suggestion_only_needs_human_verification`。

## API 草案 (API Draft)
```python
POST /api/writing-assistant/draft-revisions
Request:
{
  "draft_text": "Single-atom catalysts are the best because they show extremely high conductivity."
}
Response:
{
  "draft_text": "...",
  "revision_suggestions": [
    {
      "suggestion_type": "unsupported_claim",
      "original_excerpt": "they show extremely high conductivity",
      "suggested_revision": "they have been reported to show high conductivity",
      "warnings": [
        "unsupported_claim_needs_evidence",
        "draft_do_not_use_as_final_fact"
      ],
      "candidate_papers": []
    }
  ],
  "safety_guardrails": {
    "is_suggestion_only": true,
    "writes_db": false,
    "auto_apply": false,
    "generates_bibliography": false,
    "export_unlocked": false,
    "verified_status_changed": false
  }
}
```

## 前端草案 (Frontend Draft)
- **触发入口**: 在 WritingWorkflow 中新增 "Revise Draft" 按钮。
- **UI 展示**: 与 Comment Assistant 类似，展示为一个 List of Cards。每个卡片高亮标注 "DRAFT: Needs Evidence" 等警告标签。
- **无破坏性操作**: 彻底隐藏 "Apply Changes" 或 "Rewrite File" 等危险操作。提供简单的 Copy 按钮方便用户自行整合到剪贴板。

## 测试计划 (Test Plan)
- **单元测试**: 针对 `DraftRevisionAssistantService`。
- **验证项**: 
  - Blank `draft_text` 抛出异常或返回空结果。
  - Mock 出带有 unsupported claim 的段落，断言返回的 warning 中包含 `unsupported_claim_needs_evidence`。
  - Mock 出带有 unverified 候选证据的段落，断言候选文献保持原样且不会变成 confirmed fact。
  - 断言所有的 guardrail flags 均为 false。

## 风险清单 (Risks)
- LLM 或逻辑层可能会过度改写用户的原意，或不恰当地合成事实 (Hallucination)。所以 "suggestion-only" 的警告是至关重要的防御底线。
