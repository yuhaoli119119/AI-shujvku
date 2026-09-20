# MCP 单篇论文执行指令

> 面向执行 AI 的单篇论文处理指令，基于当前已上线 MCP 工具**论文处理面 69 个**（生产端点 `https://dft.researchlife.top/mcp`，Bearer key 鉴权）。
> 工具名与参数核对自生产真实 schema 与仓库 `backend/app/mcp/tool_contracts.json`（契约 84 项 − `app/mcp/tool_surface.py` 的 `EXCLUDED` 15 项 = 论文处理面 69 项），并与实际会话暴露的工具列表逐项一致（核对于 2026-09-16）；本轮仅修改本说明文件，未开发/未发布/未运行生产写入。
> 固定流程：**选择论文 → PDF解析确认 → 图表(figure+table)审核 → DFT提取 → 证据核验 → 单篇去重/归类/标准化 → 任务导出 → 文件读取与验收**。
> 标注规则：[已验证] = 本轮或既有调用记录已端到端跑通（如 R5/R16/R19 隔离与生产调用）；"返回字段映射待补证" = 工具可用（部分已有生产执行证据），但精确响应字段名未在已引用证据中找到，正式使用时按响应实际读取，不推断工具不可用、不重写生产验证。

## 通用铁律（全流程适用）

1. **身份重算只改派生身份字段**（`identity_version`/`subject_key`/`observation_key`/`identity_payload` + 审计），**不等于科学去重完成**；执行 AI 用 `review_paper_identity` + `read_paper_page` 按存储证据核查，确需人工裁决的项单列（见末尾"缺口"）。
2. **纠正提案 ≠ 应用成功**：`propose_dft_result_correction` 只创建 pending 提案；须 `approve_corrections_batch(correction_ids=[...])` 才落库（`approve_correction` 单条入口已在论文处理面排除）。
3. **字段核验不改原始科学值**：`apply_ai_verification_batch` 只写审核状态（accept/defer/reject），**永不改原始 DFTResult 字段**（value/unit/材料等）。科学字段更正按问题类型使用既有受控入口——`propose_dft_result_correction → approve_corrections_batch`（提案+批量批准，保留原始数据）或 `repair_dft_audit_issue(action=update_dft_fields)`（审计问题快速路径，需 DFT 写身份）等——遵守各入口的证据、权限、锁与回读要求，**不声称只有一条路径**。
4. **正式提交以 `apply_ai_verification_batch` 为准**：它在同事务持久化 `request_id` 回执并新会话回读存储审核状态（逐项隔离校验）。**正常批次只调一次** `apply_ai_verification_batch`，返回即含独立真实回读——不要先 dry-run、再 apply、再逐项查询。响应丢失/结果不明时按**原 `request_id`** 调 `get_ai_verification_batch_receipt` 查回执（鉴权身份+paper_id+request_id 须匹配原 apply）；**仅修正返回的坏项时才用新 `request_id`**，禁止换新 `request_id` 盲目重交整批。`submit_ai_verification_batch(dry_run=true)` 仅可选预校验，不是正式提交入口。
5. **不确定数据不进入训练集**：blocked/来源不明/单位不明/身份不完整的行不进导出（`export_paper_ml_dataset` 默认 `ready_only=true` 只含 eligible）。
6. **不使用已禁用/兼容桩入口**：`review_paper`、`verify_dft_result`、`verify_dft_results_batch`、`reject_dft_result`、`reject_dft_results_batch` 等共 15 个 `EXCLUDED` 入口（见末尾"排除入口"）。保留服务器完整工具集合，不改全局配置或其他客户端。
7. **标准化 ≠ 覆盖原始值/单位**：`review_paper_identity.standardization` 只给问题与规范单位建议；改写必须走受控更正入口（见铁律 3），保留原始数据，不自动覆盖。
8. 逐字引文（`evidence_text`/`evidence_quote`）由执行 AI 用 `read_paper_page` 读取并核对真实 PDF 证据，**不代表必须人工操作**。
9. **条件操作不是每篇必跑**：`merge_table`/`delete_table`/`create_figure_from_bbox`/`recrop_figure`/身份重算 `apply_paper_identity_rematerialization`/纠正应用 `approve_corrections_batch`/`repair_dft_audit_issue` 等，仅在发现对应问题且已获任务授权时才执行；下方工具清单是可用集合，**不是逐个调用清单**。
10. 所有 `paper_id` 为 UUID 字符串；证据定位用 `evidence_payload`（`table_id` + 0-based `source_row_index`/`source_column_index`，或逐字 `evidence_text` + `page`）。

---

## 阶段 1 · 选择论文

**首选**：`query_papers`
- **参数**：`{q?, year?, journal?, has_dft_results?, has_writing_cards?, sort_by="year_serial", sort_order="desc", limit=20, offset=0}`（全可选；`{}` 即返回一页）。默认 `sort_by="year_serial"` 是按年份序号排序，**不是"最近入库"**；要最近入库用 `sort_by="created_at", sort_order="desc"`。
- **参数来源**：用户关键词填 `q`；或 `has_dft_results=true` 限定有 DFT 的论文。
- **成功判据**：`returned>0`、`items[]` 非空；论文 ID 取 `items[].paper_id`（== `items[].id`，同一 UUID）。[已验证]
- **写库/文件**：无（只读）。
- **备用**：`get_paper(paper_id)` 取该篇完整解析数据（只读）。
- **blocked**：`returned=0` → 换关键词或扩 `limit/offset`；无匹配则停止。

## 阶段 2 · PDF 解析确认

**首选**：`get_paper_processing_status(paper_id, max_catalysts=30)`
- **参数来源**：`paper_id` 取自阶段 1。
- **成功判据**：`status=ok`；`paper.paper_id` 与请求一致；`stages` 含主文与显式关联 SI 的解析状态。[已验证]
- **写库/文件**：无（只读）。
- **未解析 vs 未入库（分开处理）**：
  - **未入库**（论文不在库）：无单篇按 `paper_id` 触发解析的 MCP 入口。须先将 PDF 放到服务器侧目录（非 MCP 操作），再 `ingest_pdf_batch(folder_path, recursive=true, limit=20, only_unparsed=true)` 按文件夹批量入库。
  - **已入库但解析未完成/失败**：`get_parse_status(job_id)` 按 `job_id` 查解析作业（**无 paper_id 级解析状态入口**；论文级状态见 `get_paper_processing_status`）。解析未完成则停止后续阶段。
- **blocked/缺口**：无单篇解析触发入口是已知 MCP 缺口（见末尾）。
- **注意**：`stages.figure_review` 的 `stale`/`unresolved_count` 来自 v1 图表快照；是否真正完成以阶段 3 的 V2 任务（`get_paper_review_task` 的 `status.figure_reading_coverage` 与 `dft_gate_allowed`）为准，两者口径不同。

## 阶段 3 · 图表（figure + table）审核

> **当前生产流程是 V2**（prompt_version `paper-review-v2-2026.09.16.2`，注册表 `figure-types-2026.09.16-v1`）。v1 的 `get_chart_review_task` + `review_figure` + `finalize_chart_review` 流程已被 V2 正式批量写入取代；v1 入口仍可读（用于查看遗留 `unresolved_actions`），但**不得**再把 v1 单独裁决当作图表阶段完成。

**首选（读取 V2 任务）**：`get_paper_review_task(paper_id)` — 返回主文 + 显式关联 SI 的对象版本、任务指纹、标准图表类型注册表、当前 `prompt_version` 与完整扁平 schema。
- **参数来源**：`paper_id` 同前。
- **成功判据**：`schema_version=paper_review_task_v2`；对每个 figure/table 取 `object_version`；整体取 `task_fingerprint`。`status.figure_reading_coverage` 显示 main 模块解读覆盖（如 `0/6`）。
- **写库/文件**：读不写。

**V2 正式写入**：`apply_paper_review_batch(paper_id, request_id, task_fingerprint, figure_actions?, table_actions?, figure_readings?, final_state?, notes?)`
- **参数来源**：`task_fingerprint` 与逐对象 `expected_object_version` 均取自 `get_paper_review_task`；`request_id` 由调用方生成（8–64 字符，`^[A-Za-z0-9._:-]+$`）。
- **figure_actions.action** ∈ {KEEP, UPDATE, RECROP, COMPOSE_PAGES, CREATE, DELETE_FALSE_POSITIVE, HOLD}；参考文献列表/页眉/出版社标志/CrossMark 等版面碎片用 `DELETE_FALSE_POSITIVE`；同页裁图问题用 `RECROP`；跨页同一逻辑图用 `COMPOSE_PAGES`（按 `pages` 顺序给 `bbox_norm`）；数据不足用 `HOLD` 但继续处理其他有效项。
- **figure_readings** 每项必填 `summary_zh`、`detailed_explanation_zh`、`evidence_locators`（≥1，含 `quote`+`page` 或 `visual_observation`）、`typing`（`figure_type_primary`/`figure_types`/`type_confidence`/`type_evidence`）。**标准类型不得只按标题关键词猜测**；`panel_types` 记录了子图时 `subfigures` 必须逐个 label 一一对应并写非空中文 `description`，禁止只写 "(a)"、"(b)" 或用 "(a-b)" 代替真实子图。
- **table_actions.action** ∈ {KEEP, UPDATE, CREATE, DELETE_FALSE_POSITIVE, HOLD}；`UPDATE` 需给证据。
- **成功判据**：一次论文**只正式调用一次**；返回按 savepoint 分组的 `applied`/`unchanged`/`held`/`rejected` 四类结果 + `authoritative_readback`。**必须检查四类结果与权威回读**，不得把 HTTP/MCP 成功当作全部写入成功。
- **重试/丢失**：正常响应直接看 `authoritative_readback`；**仅**响应超时或丢失时才用原 `request_id` 调 `get_paper_review_receipt(paper_id, request_id)`，禁止换新 `request_id` 重交不同载荷。
- **写库/文件**：写 figure/table 对象、标准类型与中文解读 + 审计。

**辅助只读入口**：
- `get_figure_type_registry()` — 版本化标准图表类型注册表（稳定 key/中文名/双语别名/父类）。
- `search_figures(query?, figure_type?, paper_id?, year?, material_system?, catalyst?, dft_condition?)` — 按标准类型或中英别名检索整图与复合图子图类型。
- `get_figure_image(paper_id, figure_id)` — 取当前裁剪图为 MCP 图像块（`paper_id` 须拥有 `figure_id`，含显式关联 SI）。
- `render_paper_page(paper_id, page, dpi?)` — 渲染真实 PDF 页为图像，用于裁图/版式核对。

**单对象工具（条件执行，非每篇必跑）**：`review_figure` / `recrop_figure` / `create_figure_from_bbox` / `update_table` / `create_table` / `merge_table` / `delete_table` 仍可用于单对象修复；`resolve_chart_review_actions` / `finalize_chart_review` 属 v1 入口，仅在处理遗留 v1 未解决动作时使用，**不能替代 V2 批量提交**。
- **blocked**：缺对象 → `create_figure_from_bbox`/`create_table` 补；证据不足 → `HOLD`，不伪造 verified。

## 阶段 4 · DFT 提取（新候选物化 + 审核任务读取）

`get_dft_review_task` **只读取已有任务，不能代替提取**。新数据须先物化为 DFTResult 候选。

**新候选物化**：`read_paper_page(paper_id, page_start, page_end?)` 核对图表/表格/原文 → `import_analysis(paper_id, source, source_label?, raw_text?, raw_payload?, auto_apply_review_rules=true, reviewer?, write_lock_token?)`
- **载荷**：`raw_payload` 含 object-level review audit（`target_type`/`target_id`/`field_name`/`decision`/`evidence_location`/`corrected_value`）或自由文本。**DFT `decision=new_candidate` 可物化一条未核验 DFTResult 候选**（取得新 `dft_result_id`/record_id）。新 record_id 的确切返回字段——返回字段映射待补证（未在已引用证据中找到逐字段名，按 `import_analysis` 响应实际读取；工具可用，不推断不可用）。
- **何时仅保存意见**：`decision` 为普通 PASS/REVISE/REJECT（非 new_candidate）→ 仅作意见保留（`no_ai_overwrite`，待人工，非权威验收）。
- **何时物化未核验记录**：`decision=new_candidate` → 物化未核验 DFTResult 候选，取得新 `record_id`，进入阶段 5 字段核验。
- **写库/文件**：`import_analysis` 写候选/审计意见（非权威）；`auto_apply_review_rules=true` 自动物化 object_review_audit 候选。

**对既有 run 重算**：`apply_analysis_review_rules(run_id, reviewer?, write_lock_token?)` — 对 `auto_apply_review_rules=false` 导入的 run 物化 object_review_audit 候选，或新增候选后重算。

**审计问题快速修复（替代/补充路径，条件执行）**：`repair_dft_audit_issue(issue_id, action, repair_payload, reason, evidence_payload)` — actions ∈ {create_missing_dft, update_dft_fields, link_existing_duplicate, mark_needs_user_decision}（需 DFT 写身份）。仅在审计问题驱动且获授权时用；与 `propose_dft_result_correction→approve_corrections_batch` 是不同受控路径（见铁律 3）。

**SI 拥有候选生命周期**：`resolve_supplementary_dft_candidate(main_paper_id, support_candidate_id, status, reason?, canonical_dft_result_id?)` — status ∈ {ignored, replaced, written_back, needs_human}；replaced/written_back 须给 `canonical_dft_result_id`。

**审核任务读取**：`get_dft_review_task(paper_id, catalyst_sample_id? | dft_result_ids?)` — 读取本篇 DFT 审核任务（`target_count`/`dft_result_ids[]`/`review_mode`/`source_pdf_inventory`/`figure_table_review`）。[已验证] 可选 `catalyst_sample_id`（取自阶段 2 催化剂分组）或 `dft_result_ids`（含新物化的 id）精确重审。
- **写库/文件**：`get_dft_review_task`/`read_paper_page` 只读；`import_analysis`/`apply_analysis_review_rules`/`repair_dft_audit_issue`/`resolve_supplementary_dft_candidate` 写库。
- **blocked**：某条证据不足 → 标 blocked，不阻塞其他有效行；新候选无证据基础 → 不物化。

## 阶段 5 · 证据核验（正式字段提交）

正式 DFT 字段提交统一路径：
`get_ai_verification_record_tasks` → 按真实 PDF 核对证据 → （可选）`submit_ai_verification_batch(dry_run=true)` 预校验 → `apply_ai_verification_batch(paper_id, request_id, submissions)` → 检查逐项结果与权威回读。

**首选（读任务）**：`get_ai_verification_record_tasks(paper_id, limit=20, cursor?, include_blocked=false)` — 只读分页取 DFT 记录 bundle（每条含必决字段、快照、版本、locator、PDF/SI 页候选）。用 `next_cursor` 翻页（不用 offset，已完成字段消失不会跳记录）。`ai_blocked` 字段为非可操作上下文，**不得重交**。[已验证]
- **备用（另一分页）**：`get_ai_verification_tasks(paper_id, limit=20, offset=0, recover_evidence=true, target_type?, include_blocked=false)` — pending 内容核验任务页（limit+offset）。[已验证]

**核对证据**：`read_paper_page(paper_id, page_start, page_end?)` 读 PDF 原文页（权威页源，不写库）。[已验证]

**可选预校验**：`submit_ai_verification_batch(paper_id, submissions, dry_run=true)` — 零写入，仅按确定性闸门（页/文本/定位器/快照/数值单位/冲突）校验 `submissions` 形状。**不作为正式提交入口**（正式提交见 `apply_ai_verification_batch`）。

**正式应用**：`apply_ai_verification_batch(paper_id, request_id, submissions)` — `request_id` 由调用方生成（UUID），最多 20 条/批。`submissions[]` 每项严格 schema（`additionalProperties=false`）：`target_type`(DFT 用 `dft_results`)、`target_id`、`field_name`、`decision`(accept|defer|reject)、`confidence?`、`evidence_text?`、`page?`、`proposed_value?`、`reasoning_summary?`、`counter_evidence_text?`(reject 必填)、`expected_target_fingerprint`、`expected_write_version`、`source_paper_id?`、`evidence_paper_id?`、`table_id?`+`source_row_index?`+`source_column_index?`(须同时提供)、`blocked_reasons?`(defer 必填)。
- **成功判据**：服务端独立校验每项 → 同事务持久化 `request_id` 回执 + 审核写入 → 提交 → 新会话读存储审核状态（权威回读）。`accept`=证据支持核验；`defer`须给 `blocked_reasons`；`reject`须给 `counter_evidence_text`。**永不改原始 DFTResult 字段**。
- **写库/文件**：写 `ai_verified`/`rejected`/`exception` 审核状态 + `request_id` 回执；不写 human_verified；不改 value/unit/材料。
- **重试/丢失**：响应丢失/结果不明 → `get_ai_verification_batch_receipt(paper_id, request_id)` 按**原 `request_id`** 查回执（鉴权身份+paper_id+request_id 须匹配原 apply）；**禁止换新 `request_id` 盲目重交**。正常成功响应已含回读，不机械重复查回执。

**收尾**：`finalize_ai_verified_dft_records(paper_id, result_ids? | issue_ids?)` — 幂等；仅对必决字段已权威核验的记录收尾（事务锁后重读+活闸门复检）。[已验证]
- **blocked**：`ai_blocked` 字段不重交；记录整体 blocked 跳过，不进训练集。

> 注：`apply_ai_verification_batch`（字段核验正式应用）**已有正式生产执行证据**——R5 direct_apply 工作流以 `paper_id`+`request_id`+`submissions` 提交，服务端逐项隔离校验、同事务持久化 `request_id` 回执并新会话权威回读（R10/R14 指令：正常批次只调一次、返回独立真实回读，响应丢失才用原 `request_id` 查 `get_ai_verification_batch_receipt`，仅修正坏项用新 `request_id`）。**逐项结果与回读的精确响应字段映射——返回字段映射待补证**（未在已引用证据中找到逐项字段名，正式使用时按响应实际读取；不推断工具不可用、不重写生产验证）。
> 注意区分：字段核验 `apply_ai_verification_batch` 与身份重算 `apply_paper_identity_rematerialization` 是**不同工具**；后者本轮隔离测试跑过 `dry_run` + 3 行 fixture formal apply（见阶段6），不要混为前者。

## 阶段 6 · 单篇去重、归类、标准化

**首选（只读检测）**：`review_paper_identity(paper_id, max_items=30)`
- **成功判据**：`schema_version=paper_identity_review_v1`、`read_only_transaction=true`、`scope.dft_records` 与 DB 一致；返回 `identity.{incomplete,not_materialized,complete_records,records[]}`、`duplicates.{exact_duplicates, same_subject_multi_observation}`、`field_conflicts.count`、`classification.unclassified`、`standardization.{unit_missing,unit_mismatch,out_of_scope}`、`catalysts.{total,items[]}`、`next_steps[]`。[已验证]
- **写库/文件**：无（只读事务）。
- **去重核查**：执行 AI 按 `exact_duplicates`（同 `observation_key`=平台身份语义确认重复）+ `read_paper_page` 核对证据；`same_subject_multi_observation`（同 `subject_key` 不同 `observation_key`）**不构成重复结论**，须按证据区分构型/步骤/条件。确需人工裁决的（如 manual+evidence_insufficient 冲突、身份 incomplete 无法补齐）单列见"缺口"。

**受控身份重算**：`apply_paper_identity_rematerialization(paper_id, expectations, reason, dry_run=true)`
- **参数来源**：`expectations[]` 的 `dft_result_id`+当前 `identity_version`/`subject_key`/`observation_key` 原值，取自 `review_paper_identity.identity.not_materialized.items[]` 或 `records[]`。
- **成功判据**：`scope.fields_written=[identity_version,subject_key,observation_key,identity_payload]`、`scientific_fields_untouched=true`；`dry_run=true`→`status=dry_run`/`written=0`；formal→`status=applied`/`written>0`/`readback.ok=true`；重试→`status=no_changes`/`written=0`。[已验证]
- **写库/文件**：仅写 4 派生身份字段 + 每变更行 1 条 `audit_logs(action=rematerialized_dft_identity_v2)`；不改科学字段、不删除/合并。先 `dry_run=true` 复核；跨论文/stale/碰撞整体拒绝。

**归类落库**：`assign_dft_reaction_label(paper_id, record_id, reaction_type, evidence_source_paper_id, evidence_page, evidence_quote, expected_target_fingerprint, dry_run=true)`
- **参数来源**：`record_id`、`reaction_type` 建议取自 `review_paper_identity.classification.unclassified.items[].{dft_result_id, suggested_reaction_type}`；`evidence_quote` 由 AI 用 `read_paper_page` 取逐字引文（非必须人工），`evidence_page`/`evidence_source_paper_id` 指向原文。
- **成功判据**：服务端按记录重算分类与校验结论；拒绝覆盖既有不同归属；要求记录已通过证据核验；幂等；`dry_run=true` 零写。**已有生产应用证据**：R5 `assign_dft_reaction_label(dry_run=false)×3` → 3/3 `applied`，审计 `assign_dft_reaction_label` 28→31（即 28+3 条生产应用）。精确逐项响应字段映射——返回字段映射待补证。
- **写库/文件**：写 `reaction_type`/`reaction_validation_status`。**这是反应标签归类入口，不等于全部性能分类完成。**

**字段更正（标准化/数值/单位/材料，保留原始数据）**：`propose_dft_result_correction(paper_id, dft_result_id, field_name, proposed_value, reason, confirm_correction_proposal, evidence_payload?)` → `approve_corrections_batch(correction_ids=[...])`
- **参数来源**：`dft_result_id`/`field_name` 取自 `review_paper_identity.standardization.{unit_mismatch,out_of_scope}` 或核验问题；`evidence_payload` 含 `table_id`+行/列索引或逐字引文+页码。先 `get_correction_queue`/`get_correction_detail` 复核 pending 提案。
- **成功判据**：`propose_*` 返回 pending `correction_id`（**提案 ≠ 应用**）；`approve_corrections_batch` 逐项跳过非 pending/失败项并回报后落库。**不自动改写**，证据不足保持原值；标准化是建议，不覆盖原始值/单位。
- **写库/文件**：`approve_corrections_batch` 应用更正；顶层结构/机制/写作卡字段需 `write_lock_token`（`acquire_module_write_lock` 取得，用完 `release_module_write_lock`）。
- **备用**：`reject_corrections_batch(correction_ids=[...], reason?)` 拒提案（单条 `approve_correction`/`reject_correction` 已在论文处理面排除）。

**只读辅助**：`get_dft_audit_issues(paper_id, statuses?, issue_types?, limit=50, cursor?, sort_direction="desc")`（审计问题队列，只读不修复）；`get_review_conflicts(paper_id, target_type?, target_id?, field_name?, include_non_conflicts=false, limit=200)`（字段级冲突聚合，只读不下裁决）；`append_note(paper_id, content, field_name?, page?, section_title?, quoted_text?)`（附共享评审备注）。
- **blocked**：身份 incomplete / 字段冲突 manual+evidence_insufficient / reaction_type 无证据 → 保持未定，不进训练集，不阻塞其他有效行。

## 阶段 7 · 任务导出

**首选**：`list_ml_export_tasks()` → `export_paper_ml_dataset(paper_id, task, ready_only=true, include_payload="none", artifact_ttl_hours=24)`
- **参数来源**：`task` 取自 `list_ml_export_tasks` 的 `tasks[]`（每项含 `task` 键如 `SRR_LiS:adsorption_energy`、required/optional features、允许属性/单位、download matrix）。[已验证]
- **成功判据**：`counts.exported_rows` 为该 task 实际训练行数；`downloads.artifact.{artifact_id, expires_at, formats.{csv,json,manifest}.{byte_count, sha256}}`。[已验证]
- **写库/文件**：`export_paper_ml_dataset` 在受控存储生成 4 个产物文件（dataset.csv/json + manifest.json + artifact.json，TTL 默认 24h），机会式清理本论文名下已过期产物（不可恢复）。**不确定数据不进入训练集**。
- **关键口径**：`evidence_gate_eligible`（证据闸门可导出候选数，导出安全层面）**≠** `counts.exported_rows`（该 task 实际训练行数）。
- **备用**：无（全库 v2 旧版 `export_ml_dataset` 已列入 `EXCLUDED`，不作为任何流程入口）。
- **blocked**：`exported_rows=0` 表示该 task 无就绪行（不伪造数据）；换 task 或回前序阶段补证据。

## 阶段 8 · 文件读取与验收

**首选**：`read_ml_export_artifact(paper_id, artifact_id, fmt, offset=0, limit_bytes=65536)`
- **参数来源**：`artifact_id`/`fmt` 取自阶段 7 的 `downloads.artifact`。
- **成功判据**：按 `next_offset` 顺序拼接各块 `content`，其 SHA-256 == `content_sha256`（整份产物哈希，导出时固化）；重复读取返回相同 `chunk_sha256`、不重算。[已验证]
- **写库/文件**：无（只读，不重新执行 v3 构建器，不改产物）。
- **关键约束**：偏移单位为 UTF-8 字节；单块硬上限 65536 不可提高。产物绑定调用方身份；过期或缺失 expires_at 一律明确报错（`artifact_expired`/`artifact_corrupt`），不偷偷重新导出沿用原 ID。
- **blocked**：产物已过期 → 重新 `export_paper_ml_dataset` 取**新** `artifact_id`（旧 ID 不复用）。

---

## 统计字段准确含义（不要为统一数字改代码或数据）

- **登记样本**（`get_paper_processing_status.catalysts.registered_sample_count`）：本篇下登记的 `catalyst_sample` 总数。
- **承载 DFT 样本**（`dft_bearing_sample_count`）：有 `dft_results` 记录的样本数。
- **目标催化剂**（`categories_by_dft_bearing_samples.target`，headline）：`catalyst_type` 属目标类且承载 DFT 的样本数（不含无数据样本）。
- **未绑定分组**（`unbound_record_count`）：`dft_results.catalyst_sample_id` 为空或引用无效的记录数，归 `__unbound__`，**不按名称猜测归属**。
- **`review_paper_identity.catalysts_total`**：检测响应按 `catalyst_sample_id` 分组的催化剂数（受身份/重复/归类/标准化问题影响计数维度），**与 `registered_sample_count` 口径不同**。
- **`review_paper_identity.unclassified`**：`reaction_type` 缺失或为 `UNKNOWN` 的记录数（规则建议已给，落库走 `assign_dft_reaction_label`，证据不足保持未分类）。
- **证据闸门数量**（`stages.ml_export.evidence_gate_eligible`）：通过导出证据闸门的候选数（导出安全层面）。
- **任务训练行数量**（`export_paper_ml_dataset.counts.exported_rows`）：该 task 实际进入训练集的行数（`ready_only=true` 时只含 eligible 且 task 必需字段就绪行）。**eligible ≠ exported_rows**。

---

## 工具清单（论文处理面 69 = 84 契约 − 15 排除；本指令引用如下）

**主流程引用（48）**：
`query_papers`、`get_paper`、`get_paper_processing_status`、`get_paper_review_task`、`apply_paper_review_batch`、`get_paper_review_receipt`、`get_figure_type_registry`、`search_figures`、`get_figure_image`、`render_paper_page`、`get_chart_review_task`、`review_figure`、`create_figure_from_bbox`、`recrop_figure`、`resolve_chart_review_actions`、`finalize_chart_review`、`create_table`、`update_table`、`merge_table`、`delete_table`、`get_dft_review_task`、`read_paper_page`、`import_analysis`、`apply_analysis_review_rules`、`repair_dft_audit_issue`、`resolve_supplementary_dft_candidate`、`get_ai_verification_record_tasks`、`get_ai_verification_tasks`、`submit_ai_verification_batch`、`apply_ai_verification_batch`、`get_ai_verification_batch_receipt`、`finalize_ai_verified_dft_records`、`review_paper_identity`、`apply_paper_identity_rematerialization`、`get_dft_audit_issues`、`get_review_conflicts`、`assign_dft_reaction_label`、`propose_dft_result_correction`、`approve_corrections_batch`、`reject_corrections_batch`、`get_correction_queue`、`get_correction_detail`、`acquire_module_write_lock`、`release_module_write_lock`、`append_note`、`list_ml_export_tasks`、`export_paper_ml_dataset`、`read_ml_export_artifact`。

**条件入口（2）**：`ingest_pdf_batch`（新论文入库，按服务器侧文件夹）、`get_parse_status`（按 `job_id` 查解析作业，非论文级）。

**领域专用/辅助（19，非单篇主流程首选）**：`get_codex_context`、`get_codex_item`、`scan_local_pdfs`、`get_dft_review_queue`、`get_field_disputes`、`list_notes`、`propose_correction`、`repair_dft_audit_issues_batch`、`get_review_coverage`、`get_figure_reading_context`、`validate_figure_reading`、`apply_figure_reading`、`export_paper_reading_guide_html`、`get_analysis_import_status`、`materialize_ai_section_page_fragments`、`get_ai_verification_web_apply_package`、`get_content_web_review_local_verification_plan`、`read_content_web_review_page_asset`、`apply_content_web_review_local_verification`。

**排除入口（15，`app/mcp/tool_surface.py` 的 `EXCLUDED`）**：`review_paper`、`verify_dft_result`、`verify_dft_results_batch`、`reject_dft_result`、`reject_dft_results_batch`、`export_ml_dataset`、`approve_correction`、`reject_correction`、`retrieve_evidence`、`plan_multi_paper_evidence`、`compare_papers`、`scan_duplicate_dois`、`get_paper_knowledge`、`create_share_token`、`cleanup_unused_figure_assets`。

> 48+2+19=69，与 84−15 一致。保留服务器完整工具集合，不改全局配置或其他客户端。

---

## 遗留事项（分三类，不统称"MCP 路径缺口"）

### A. 真正缺少的 MCP 能力
1. **无单篇解析触发入口**：无按 `paper_id` 触发 PDF 解析的 MCP 工具；新论文须服务器侧放 PDF + `ingest_pdf_batch(folder_path)`（非 MCP）；`get_parse_status` 按 `job_id` 查，论文级解析状态只在 `get_paper_processing_status`。
2. **新论文未入库时的服务器侧 PDF 放置**：非 MCP 操作，须人工/运维完成。

### B. 已有能力但响应映射或使用证据待补（工具可用，不推断不可用）
3. **`import_analysis` decision=new_candidate 物化的新 record_id 返回字段**：契约来自工具描述（可物化未核验 DFTResult 候选），精确返回字段映射——待补证，正式使用时按响应实际读取。
4. **`apply_ai_verification_batch` 逐项结果与回读的响应字段映射**：已有正式生产执行证据（R5 direct_apply：request_id+回执+权威回读；R10/R14 指令），但精确逐项字段名未在已引用证据中找到——待补证，按响应实际读取。
5. **`assign_dft_reaction_label` 逐项响应字段映射**：已有生产应用证据（R5：3/3 applied，28→31），精确响应字段映射——待补证。

### C. 科研证据不足或明确需要人工裁决（非 MCP 能力缺口）
6. **字段级 manual+evidence_insufficient 冲突**：无自动裁决入口，须人工回原文（`get_review_conflicts` 只读列出）；能补证据则走受控更正入口（铁律 3），不能则保持 blocked。
7. **身份 incomplete 行**：无自动回填入口，须 AI/人工回原文补证据；补不齐则 blocked，不进训练集。
8. **reaction_type 赋值无证据基础**：`assign_dft_reaction_label` 需逐字引文+页码+target fingerprint；无证据则保持未分类，不自动归类。
9. **ICOHP 等表格单元证据冲突**（如 B0102 主文 p8 vs Table S7 数值不一致）：保持 blocked，未补录、未建记录（R5 已判定先例）。

> 以上 B 类：schema 存在 ≠ 业务端到端走通；正式执行时须用真实响应核对返回路径。A 类是真正缺能力；C 类是科研证据/人工裁决，非 MCP 可解。

## 下一篇试运行应验证的最短调用链

> **适用前提**：该链仅适用于 **PDF 解析、图表审核（figure+table）与 DFT 提取已实际完成且有证据** 的论文（即前置阶段 1–4 已落地）。它**不是新论文完整处理流程**：前置阶段未完成时，必须先完成对应阶段（解析→图表→提取），**不得跳过**直接进入核验/导出。新论文须先入库+解析+图表审核+提取，再进入此链。

选一篇已有 DFT 的论文，按下列顺序最少调用即可覆盖主流程关键节点（含正式 apply 回执与导出验收）：

1. `query_papers({has_dft_results:true, limit:5})` → 取 `items[].paper_id`
2. `get_paper_processing_status(paper_id)` → 确认解析/催化剂/stages（[已验证]）
3. `get_dft_review_task(paper_id)` → 取 `dft_result_ids[]` 与审核任务（[已验证]）
4. `get_ai_verification_record_tasks(paper_id)` → 取一条记录的 pending 字段 bundle + `expected_target_fingerprint`/`expected_write_version`/证据候选（[已验证]）
5. `read_paper_page(paper_id, page_start)` → 核对该字段真实 PDF 证据（[已验证]）
6. `apply_ai_verification_batch(paper_id, request_id, submissions)` → 正式提交一条 accept（带 `evidence_text`/`page`/`expected_target_fingerprint`/`expected_write_version`）；读响应逐项结果与权威回读；**记录 `request_id`**（[有生产证据，响应字段映射待补证]）
7. `review_paper_identity(paper_id)` → 身份/去重/归类/标准化检测（[已验证]）
8. `list_ml_export_tasks()` → 取目标 `task`（[已验证]）
9. `export_paper_ml_dataset(paper_id, task)` → 取 `downloads.artifact.artifact_id` + `counts.exported_rows`（[已验证]）
10. `read_ml_export_artifact(paper_id, artifact_id, "csv")` 分块读取 → 拼接 SHA == `content_sha256`（[已验证]）

> 该链用 10 个工具覆盖：选择→状态→DFT读取→核验→证据→正式apply(回执)→身份检测→导出→读取验收。
> **注**：该链起点是阶段 4（DFT 提取）之后。若该篇图表为 V2 未完成（`get_paper_review_task.status.dft_gate_allowed=false`），必须先按阶段 3 走 V2 图表批量提交，不能跳过。
> 条件操作（图表补建/表格合并/身份重算/纠正应用）仅在步骤 2/3/7 发现对应问题时才追加，不纳入最短链。

---

*基于生产真实 schema（2026-09-12 `tools/list` 工具面存档）+ 仓库 `app/mcp/tool_contracts.json` 与 `app/mcp/tool_surface.py`（2026-09-16 复核：84−15=69）+ 既有调用记录（R3/R5/R10/R14/R16/R19）撰写；本轮仅修改本说明文件，未开发/未发布/未运行生产写入。*
