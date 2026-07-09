# 离线 Web AI 图表证据整理 + DFT 终审流程

## 结论

直接采用“两阶段、系统自动执行第二版”：

1. 图表证据整理：Web AI 只返回图表审核 JSON；系统校验后自动处理高置信度的图片重裁、图片信息补全、表格更新/创建；本地 AI 只负责确认 payload、调用 apply、读回核验。
2. DFT 专项审核：只使用已经校验并回写后的图表证据快照，再结合系统原有 DFT candidates 做终审；Web AI 仍只返回 DFT 审核 JSON，本地 AI/系统受控导入。

## 阶段 1：图表证据整理

新增材料包：

- `POST /api/papers/{paper_id}/evidence-review-bundle`
- 默认包含原 PDF、当前 figure crop、当前 table markdown、page geometry、返回 schema、返回模板和提示词。

Web AI 返回：

- `schema_version = offline_figure_table_evidence_review_result_v1`
- `figure_actions`: `KEEP / RECROP / CREATE / REJECT / NEEDS_HUMAN`
- `table_actions`: `KEEP / UPDATE / CREATE / MERGE / DELETE / NEEDS_HUMAN`
- `dft_evidence_candidates`: 只记录图表中的 DFT 证据候选，不等于 verified DFT 数据。

系统处理：

- `POST /api/papers/{paper_id}/evidence-review-result/validate`
  - 只校验，不写库。
  - 校验 paper identity、bundle fingerprint、object id、evidence id、bbox、coverage、表格 markdown 完整性。
- `POST /api/papers/{paper_id}/evidence-review-result/apply`
  - 只在 `overall_status = completed` 后记录 curated evidence snapshot。
  - 自动执行高置信度操作：
    - figure `KEEP` metadata
    - figure `RECROP`
    - figure `CREATE`
    - table `UPDATE`
    - table `CREATE`
  - 永不自动执行：
    - table `MERGE`
    - table `DELETE`
    - figure `REJECT`
    - `NEEDS_HUMAN`

## 阶段 2：DFT 专项审核

现有 DFT bundle 已扩展：

- `POST /api/papers/{paper_id}/dft-review-bundle`
- 新增 `parsed/curated_figure_table_evidence_snapshot.json`
- manifest 新增：
  - `figure_table_evidence_review_status`
  - `figure_table_evidence_snapshot_fingerprint`

规则：

- 如果 `figure_table_evidence_review_status != applied`，Web AI 不能把图表来源 DFT 值判成确定通过。
- DFT bundle fingerprint 已包含 curated evidence snapshot；图表证据变化后，旧 DFT 审核 JSON 会因 fingerprint 不匹配而失效。

## 主要漏洞与当前防护

1. Web AI 伪造或复用旧 JSON
   防护：`paper_id / paper_code / bundle_fingerprint` 全部校验。

2. 图片 bbox 越界或坐标体系不一致
   防护：schema 限制 `bbox_norm` 在 0 到 1；系统按 PDF page geometry 转成真实坐标并裁剪，Web AI 不能直接上传图片覆盖。

3. 表格半截更新导致证据退化
   防护：`UPDATE / CREATE` 必须返回完整 markdown table；系统拒绝明显非表格的 markdown。

4. Web AI 把图表候选当成 verified DFT
   防护：阶段 1 只允许写 `dft_evidence_candidates`；阶段 2 重新做 DFT 审核。

5. 删除/合并类破坏性操作误执行
   防护：`MERGE / DELETE / REJECT / NEEDS_HUMAN` 永不自动执行，只能留给本地 AI 或授权确认者处理。

6. 阶段 1 未完成就进入 DFT 终审
   防护：DFT bundle 暴露 `figure_table_evidence_review_status`；提示词要求未 applied 时只能 `NEEDS_HUMAN / uncertainties`。

## 当前保守限制

- table `KEEP` 目前主要通过整体 applied audit 表达，不单独创建每个 table 的 no-op 审核记录。
- 跨页表格自动合并仍保守地交给本地 AI/人工，不做自动 merge。
