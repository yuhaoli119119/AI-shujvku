# D5-1 Manuscript Comment Assistant Plan

## 目标与非目标 (Goals and Non-Goals)
- **目标**:
  - 为用户输入的文本段落提供初步的审查与建议 (Comment Suggestions)。
  - 在每条建议中附加可能的证据支持 (Citation Candidates)。
  - 严格展示证据状态，特别是区分 `safe_verified` 和未验证状态。
  - 所有输出均作为 `draft/comment/suggestion`，仅用于人工审查。
- **非目标**:
  - 不提供自动插入正文 (Auto-insert)。
  - 不生成最终引用列表或 Bibliography。
  - 不做任何数据库写入或批量晋升 (Bulk Promotion)。
  - 不导出任何最终文档。

## 用户输入与输出 (User Input and Output)
- **输入**: 
  - `paragraph_text` (String): 用户粘贴的文本段落。
- **输出**:
  - `suggestions` (List): 包含建议文本、对应的候选文献 (`candidate_papers`)、以及状态与警告信息。
  - 必须携带的警告标志 (Warnings)，提示这些仅仅是未经验证的建议。

## 证据状态展示 (Evidence Status Display)
- 借用现有的 `WritingCitationCandidateService` 返回文献候选列表和证据状态。
- 如果状态不是 `safe_verified`，则必须在前端与后端强制添加 warning："suggestion_only_needs_human_verification"。

## Suggestion-only Guardrail (防卫机制)
1. **只读性 (Read-only)**: 后端接口只查询数据，调用 `CitationCandidateService.recommend`，不更新 DB。
2. **状态透传与警告**: 强行在 suggestion 级别加入 `warnings` 数组。如果在 candidate 里有不安全的状态，全局加上不可作为事实引用的警告。
3. **Draft 标记**: 明确包含一个 flag `is_suggestion_only: True`。

## API 草案 (API Draft)
```python
POST /api/writing-assistant/suggest-comments
Request:
{
  "paragraph_text": "The catalyst shows high activity."
}
Response:
{
  "paragraph_text": "...",
  "suggestions": [
    {
      "type": "draft_comment_suggestion",
      "text": "Consider citing evidence for the claims in this paragraph.",
      "candidate_papers": [ ... ], // From WritingCitationCandidateService
      "warnings": [
        "suggestion_only_needs_human_verification",
        "draft_do_not_use_as_final_fact"
      ]
    }
  ],
  "safety_guardrails": {
    "is_suggestion_only": true,
    "writes_db": false,
    "auto_insert": false,
    "generates_bibliography": false
  }
}
```

## 前端入口草案 (Frontend Draft)
- 建立 `WritingWorkflow` 组件。
- 文本框允许用户粘贴 draft 段落。
- 点击 "Analyze / Suggest" 按钮。
- 下方卡片列出 suggestions 和 candidates，卡片头部用醒目的黄色或橙色标示 `DRAFT / SUGGESTION ONLY`。

## 测试计划 (Test Plan)
- **后端**: 
  - 编写 `test_d5_1_manuscript_comment_assistant.py`。
  - 断言 `writes_db` 永远为 `False`。
  - 断言 `warnings` 中必定包含 `suggestion_only_needs_human_verification` 等。
  - 断言没有抛出写入错误。

## 风险清单 (Risk List)
- 用户可能会忽视 warning，直接复制候选引用作为事实。
- RAG / Citation 返回的候选不够准确，导致 suggestion 质量低。
