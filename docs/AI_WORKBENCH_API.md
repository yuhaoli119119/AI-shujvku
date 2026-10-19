# 外部 AI 工作台读写接口

> 目标：让外部 AI 用最少的调用完成“看证据 → 逐图解释 → 来源化填表”。
> 系统不默认后台调用大模型；本页只描述人工或外部 AI 主动调用的接口。

## 1. 推荐读取流程

1. `GET /api/rebuild/templates`  
   返回通用字段、反应模板、活性中心类型、实验/DFT 类型。
2. `GET /api/rebuild/papers?q={query}`  
   搜索文献并返回 `paper_id`、文件数、图表数、数据行数。
3. `GET /api/rebuild/papers/{paper_id}/ai-work-package?view=compact`
   推荐先读小工作包：`totals/files/pages/templates_endpoint/producer_contract/table_write_contract/section_write_contract/quality_endpoint`，再按 `pages.rows/assets/sections/legacy_tables` 链接分页。每页含 `output_revision`，继续 `next_page` 直到 `has_more=false`；相同修订才可合并，409时重新只读收集，不自动重派任务。旧 `view=full`（默认）保持完整上下文字段，但数据行仍有分页，不能把首包当全量。统一 Python API 客户端保留首次上下文，并优先用轻量行续页。
4. 需要看原始证据时：
   - `GET /api/rebuild/files/{file_id}/preview`
   - `GET /api/rebuild/papers/{paper_id}/source-pdf/preview`
   - `GET /api/rebuild/assets/{asset_id}`
5. `GET /api/rebuild/papers/{paper_id}/quality?limit=100&offset=0&expected_revision={revision}`
   只读列出已存结果的 error/warning/unknown、具体对象/file/物理页、可执行修正指引；`issues_total/has_more/next_offset` 明确剩余缺项。提供表格表示、文件哈希/页数和章节来源范围摘要，不返回科学全文，不把未知项当原文未报告，也不把计数当全文完整。保存草稿继续允许；明确完整的冻结须满足来源一致性与表示门禁，独立科学验收另行判断。

## 2. 推荐写入流程

1. `POST /api/rebuild/papers/{paper_id}/assets/crop`  
   从真实 PDF 裁图；`regions` 使用 0–1 归一化坐标，可传多页实现跨页合并。
2. `POST /api/rebuild/papers/{paper_id}/assets/import`  
   批量补写/修正图表号、图注、坐标单位、材料映射、上下文和逐图解释。
3. `POST /api/rebuild/rows/import`  
   幂等批量填表；同一 `row_key` 仅在身份一致时合并，来源追加。来源化写入接口语义见第 5 节。
4. `PUT /api/rebuild/values/{value_id}`  
   单元格人工/AI修正；旧值保留在冲突历史中，不静默覆盖。
5. `POST /api/rebuild/papers/{paper_id}/ai-extract/jobs`  
   一键派发 Codex-web 目标模式线程自动提取；任务完成后自动写回 `paper_figures`，旧详情页可见图与解读。
6. `GET /api/rebuild/papers/{paper_id}/ai-extract/jobs/latest`  
   查询最近一次一键提取任务状态/进度/结果。

本提交兼容行为（以发布回执核实是否生效）：图表资产的顶层可选 `reading_explanation` 省略或为 `null` 都表示没有新增结构化解读，不清除已有富解读；源文件、页码、图片或 bbox 改变仍将旧解读标为 stale。空对象、无效结构及互相冲突的显式解读仍拒绝；`provenance.reading_explanation` / `scientific_reading` 的显式空值也仍拒绝。不得以 `null` 清空已有解读。

## 3. 外部 AI 提示词模板

```text
你是文献图表与数据整理助手。请严格按证据工作：
1) 只分析给定 paper_id 的正文和 SI，不跨论文合并来源。
2) 对每个图/表/子图分别给中文解释；复合图不能合并成一句解释。
3) 记录来源文件、PDF 页码、图表号/子图标号、图注、坐标含义、坐标单位、材料映射和上下文。
4) 看不清就保留为空，并写入 unreadable_fields；不要用 0 或猜测值占位。
5) 每个非空数值都要有 source；原文/表格/图/公式分别记录出处，图形估读最多两位小数并说明估读依据。
6) 实验/DFT、材料、位点、构型、吸附物或条件任一不同就分不同 row_key。
7) 同一结果重复出现时提交同一 row_key；系统会去重合并来源，明确值优先于估读值。
8) 冲突不要静默覆盖；提交后保留已有值和新值，只在该单元格显示冲突。
9) 不要启动解析任务，不要删除数据，不要修改用户凭据。
```

## 4. 边界

- 接口按工作台登录会话鉴权；未登录不得读写。
- `missing` 值必须留空并写 `missing_reason`。
- `estimated` 值最多两位小数，且必须有估读依据；阶段一候选代码不允许仅用 `asset_id` 代替依据。
- 不同材料、位点、构型、条件、实验/DFT 不合并。
- 冲突只影响该记录/单元格，不阻塞整篇论文。

## 5. 阶段一可靠性接口（2026-10-03 合同；实际版本按发布回执）

此节仅描述本次源码改动；生产仍以运行态为准，不代表已升级。

### 请求与字段隔离

`POST /api/rebuild/rows/import` 保留 `{paper_id, rows}` 请求形状。JSON 无法解析、无效/不存在的 paper_id、空 rows 或超过 200 行仍为请求级错误。rows 的每项、values 的每项分别校验；非法行不阻断其他行，非法字段不回滚其他字段。未知行/字段键仍拒绝该项；来源的 file_id/asset_id 必须存在且属于同一 paper_id。字段内任一来源无效时该字段整体拒绝，避免保存未经完整核验的候选。数据库基础设施异常仍失败，不伪装为局部成功。

- 不提供某个 field：不写、不清空它。
- `value_type` 默认 `explicit`；`null` 或空字符串不会自动变成 missing。
- 有意留空：`{"field_name":"energy","value_type":"missing","missing_reason":"坐标模糊"}`。raw_value/numeric_value 必须为 null 或省略，原因必须非空。
- 0 是合法值，例如 `raw_value:"0", numeric_value:0`，仍需来源。
- 非空值 raw_value 与有效来源必填。空 quote/label 不是证据；只有表格行列号也不能独立定位证据。
- estimated 的原始数值（含科学计数法）实际小数位最多 2，precision_digits 不能绕过；不自动舍入。每个来源的 estimate_basis 必须说明图、坐标与读法；asset_id 不能代替依据。
- derived 沿用 estimate_basis 字段记录公式和输入来源，每个来源均需填写；系统只验证依据存在，不替 AI 判断公式真实性。
- 明确值不舍入，原单位不换算；explicit 不能携带 source_kind=estimated/derived 的证据冒充明确值。
- 有证据的新 field_name 继续支持，不要求模板字段齐全。

### 行身份与重试

同一 paper_id 内，reaction、material、support、active_site_type、active_site、configuration、material_family、data_type 和完整 condition 参与身份；字符串保持原样，不猜测别名或单位等价。新增可选 `adsorbate`、`intermediate`、`reaction_step`、`adsorption_site`、`identity_context`，存入 condition，同名 properties 或 values 旧写法亦兼容。多处声明不一致时拒绝该行，避免把能量归给错误物种。

未知不等于相同。省略 identity_context 时，仅稳定 row_key（含系统记录过的别名）、完全相同的提交，或完全相同的事实及证据可认定重试；其他不确定内容并列保留。`identity_context` 是有依据的同一组样本/构型的稳定标识，不是必须补齐的科研字段；不能仅为合并而编造。不同 identity_context 保持分开。

后续只补交失败字段时，保留该行全部身份和 condition，使用结果中的 `row_key`；有可信 identity_context 的客户端也可沿用它。row_key 只是别名，改变它不能使完全相同的事实重复入库；重用它指向不同身份会明确拒绝。新内容不能通过换 key 强行覆盖既有内容。存在多个可匹配历史行时明确报 ambiguous_legacy_rows，不自动合并、删除或改写历史行。

所有此接口的并发写入按论文数据库行锁串行化，并保留行/字段原有唯一约束。直接 SQL 或未使用本服务的写入方不在此保证内。

### 可查询的候选与来源

`GET /api/rebuild/rows?paper_id=...` 保留原读取结构，并在值的 `conflict.candidates` 中返回全部候选：raw_value、numeric_value、unit、value_type、precision_digits、missing_reason、source_ids。source_ids 对应同一值的 sources 列表。重复候选仅合并证据 ID，不覆盖之前候选，不无限增加记录。旧 conflict 对象已有键保留；历史上已被旧代码丢弃的候选无法恢复，旧混合来源也无法凭空拆回精确关系，因此返回 unattributed_legacy_source_ids 而不猜测关联。

相同值从 estimated 收到 explicit 时提升等级及首选来源；反序提交保留明确值。相同单位下同一结果的明确值优于估读值，估读候选及来源仍保留。explicit 与 derived 的分歧不按等级自动裁决；不同明确候选或不同单位保持冲突。`evidence_history` 仅表示证据历史，不是冲突警告。实际 `value_conflict`/`unit_conflict` 不会因重试某个候选而被清除。

属性冲突保存在 `properties._rebuild_import.property_conflicts`（各属性的全部候选），正常属性及 values 继续保存。notes 的历史存于同处 `notes_history`。`_rebuild_import` 是服务保留键，客户端不可写入。身份冲突属于拒绝而非“已保留”：本次内容不写入，调用方必须保留请求并修正身份后重试。

source_key 输入仍接受，但新来源按证据内容生成稳定键，不能靠改变 source_key 重复入库，也不能重用 source_key 隐藏新证据；已有来源键不重写。

### 响应与计数（客户端必须适配）

HTTP 200 仅表示批次处理并提交完成，不表示全部导入成功。旧响应键保留，但含义收紧：

| 字段 | 含义 |
|---|---|
| rows_imported | 本次全部提交内容成功处理且有实际写入的行；不计重复、部分保存或拒绝 |
| rows_duplicate | 全部内容已存在，没有新事实/证据写入 |
| rows_partial | 部分写入或候选持久化，但仍存在字段拒绝或冲突 |
| rows_rejected | 整行未处理，例如格式或身份错误 |
| complete | 所有提交项均已处理且无当前提交冲突/拒绝；不要求整篇或模板齐全 |
| results | 每个提交行都有一项（包括拒绝），含 0-based row_index、outcome；有效行含 row_id/row_key、values/properties 结果 |
| errors | 全部需处理项，含 row_index、part、field_index/field、原因或冲突状态 |

字段 outcome 为 written / duplicate / conflict / rejected；persisted 表示该值或候选实际保存。results 长度不再能用作成功计数。前端显示四类计数和失败位置；只补交 errors 中对应内容。冲突须补充证据，原样重试不会自动裁决。请求/事务级异常时客户端不应视作成功，可按相同身份幂等重试。

本阶段无数据库结构迁移；新增历史信息复用现有 JSON 列，读取兼容。回滚旧写入服务后，它仍可能覆盖候选历史或恢复旧误计数；因此回滚窗口应暂停导入并保留数据库快照，不能把代码回滚视为数据历史回滚。

### 2026-10-03 独立验收 R1—R4 补修合同

1. **旧冲突不能由普通重试消除。** 旧 existing/incoming 中可辨识的候选转入 candidates；来源无法归属则候选 source_ids 留空，保留 unattributed_legacy_source_ids。旧未决 value_conflict/unit_conflict 另记录 legacy_unresolved，普通导入始终保持未决状态，即使某个旧候选缺少细节，也不能当成已解决。前端同时检查该标记。没有新增自动裁决或历史合并流程。
2. **身份字段先验证再提升。** values 中 adsorbate/intermediate/reaction_step/adsorption_site/identity_context 先通过字段结构、内容和来源归属校验。被拒绝的值不能进入 row.condition，也不会写成确定身份单元格。其余合法值保存于独立的身份待定行，GET/导入结果提供 identity_status=pending，元数据 pending_identity_fields 列出未定维度，identity_rejections 保留去重后的诊断；合法值结果为 outcome=pending、persisted=true，批次 complete=false，行计入 rows_partial。它们确实保存，但不得被解释成已经归属于被拒绝的物种。分析查询暂时排除这些行，其他行照常分析。
   - 恢复方式：保留全部已知行身份/condition，使用结果的 canonical row_key，补交 pending_identity_fields 中所有维度的正确值及有效来源；不必重传已保存兄弟字段。只有本接口显式创建的待定行允许据此补全原先未知维度。普通历史行仍不可通过该路径改变身份。
   - 省略坏字段并不解除待定状态。待定行不会凭相同 identity_context 混入已确认行。若明确定位已确认行，却附带不能验证的身份声明，该行本次提交拒绝并要求纠正身份后按原键重试，不修改原行；其他行继续处理。
3. **定位优先级：canonical row_key → 唯一别名 → 科学身份匹配。** 精确、唯一定位后仍校验全部身份，允许针对指定历史行补交，不合并其他重复行。只有别名匹配多行或无精确定位且有多条身份匹配时拒绝，错误中列出可供重试的 canonical keys。canonical key 不受其他行同名别名干扰。
4. **可解析原始数字与 numeric_value 一致。** 该校验此前仅限 estimated，是既有缺口，本次补到 explicit/derived。支持带符号十进制、科学计数法、合法零和外围空白。raw_value 原样保留；numeric_value 是既有 binary64 分析列，必须等于原始 Decimal 转为该列类型的正确投影，而非使用模糊容差。这样不会把合法高精度原文因浮点尾数误拒。明确非零原值下溢为零会被拒绝；可省略 numeric_value 只保留原文，不自动舍入原文或换单位。
   - 文本、区间、不等号、包含单位/误差的复合表达不自动提取代表数。保留 raw_value 并令 numeric_value=null；若同时提供数值，则仅拒绝该字段，原因 numeric_value_requires_plain_numeric_raw。不要用范围中点填充分析列。
   - 纯数字也允许 numeric_value=null；不会为了填满分析数据而推断或强制补齐。来源与估读/推导依据规则继续有效。

此次补修无需数据库结构迁移，schema 请求外形不变。新增 outcome=pending/identity_status 是对“已持久化但身份尚未确定”的明确表达，不能计入完整成功。所有补修及测试只在源码和隔离环境，尚未生产或真实浏览器验收。

### R5：请求完整性汇总合同（实际版本按发布回执）

导入顶层 complete 等于本次每个行结果 complete 的逻辑与，不以 errors 是否为空代替，不检查本请求之外的历史行。身份待定行即使 values=[]、仅补备注或正常属性，仍为 complete=false、rows_partial，并在该行结果给出 pending_reason；errors 追加 part=row、outcome=pending 的行级原因，包含 canonical row_key 和 pending_identity_fields，供定向补正。省略待补字段不能解除待定。

前端显示待定原因、行键和待补字段；即使响应其他标志不一致，rows_partial>0 或任一行 complete=false 也显示未完整处理。正确补正身份后，后续保持完整身份的空字段重试可完整成功；未参与本次请求的其他待定行不构成门禁。字段级 errors 保留，新行级诊断不改变已保存内容。


## 正式来源修正（2026-10-16）

已登记来源的归属、资产和表格坐标修正使用独立入口，不能用数值纠错追加一个新来源来代替。

- `GET /api/rebuild/sources/{source_id}`：需要工作台登录；返回完整原来源（含原 `source_key`、所属 paper/row/value UUID）、`source_revision`、首选来源 UUID 和该来源的修正历史。
- `PUT /api/rebuild/sources/{source_id}`：需要工作台登录、同源请求和 `X-CSRF-Token`。body 包含 `paper_id`、fresh `expected_revision`、新 `request_id`、非空 `correction_reason`、完整 `source` 替换对象。禁止额外字段和客户端指定 key/actor。
- `source` 必须明确给出全部字段：`file_id`（同篇已登记文件，必填）、`asset_id`、`source_kind`、`page_number`、`label`、`table_row`、`table_column`、`quote`、`estimate_basis`；除 file_id/source_kind 外均可明确为 null，不能省略。
- 来源类型沿用 explicit/estimated/derived/table/text/figure。文件和资产必须属于同一 paper，资产的 file_id 必须等于请求 file_id；来源必须有可定位的页、资产、标签或引文。estimated/derived 必须保留依据，不能替 explicit 数值改为估算来源。
- `page_number` 为原始 PDF 的 1-based 物理页，会核对原文件 SHA256、文件类型和真实页数。DOCX 来源须为 null，不能借用派生阅读 PDF 的页码。表格坐标为 0-based 非负整数，table 类型须同时给行列；其他类型须为 null。
- revision 覆盖完整存储来源、原 key 和该来源全部历史；陈旧 revision 返回 409。完整身份重算 key，撞到同一 value 的另一来源（包括旧 key 的同身份来源）返回 409，不合并、不删除。
- 原子更新只涉及该来源及所属 value 的 `conflict.source_corrections` 追加历史；source/value UUID、preferred_source_id、数值、精度、材料和原有冲突历史保持。历史保留完整 before/after、原 key、新 key、可信登录 actor、原因和 UTC 时间；无 schema migration。
- 发送前持久化完整请求。同一 value 下相同 request_id、body 和 actor 的重试不再写入，返回当前状态；变更内容或 actor 返回 409。未知结果先 GET 检查历史 request_id，再决定是否原样重试。返回 revision 是当前状态，不保证仍为首次请求落地时的 revision。
- 不存在返回 404；来源不充分、跨 paper、资产文件不匹配、非法类型/页/坐标等拒绝且不写。输入格式错误为 422，鉴权失败为 401，CSRF/Origin 失败为 403，key/revision/数据库约束冲突为 409。

`ai-work-package.write_endpoints.source_correction` 与 `source_correction_contract` 公布上述合同。此接口提供正式代码渠道；上线不代表任何论文已修正或科学验收通过，生产纠错仍须单篇授权和独立验收。


### 导入预检查与失败项补交（2026-10-18；实际可用版本以发布回执为准）

先将完整 `{paper_id, rows, request_id?, task_id?}` 发到 `POST /api/rebuild/rows/import/preflight`。会话、Origin、CSRF 与正式导入相同。它在独立事务中运行相同导入器，包含同批重复/冲突、真实来源/身份/字符数值校验，然后无条件回滚，不写科学数据、审计或幂等回执，也不占 request_id。使用已登记文件的现有物理 PDF（DOCX 仅使用已安装预览）核对页界，不生成文件。原来不带 file_id/asset 文件关联的可定位来源继续兼容，这种来源不宣称已核对物理页。

返回 `mode=preflight, simulation=true, persisted=false`；试执行 UUID 均省略，正式结果里的 `persisted/not_persisted/empty_persisted` 在预检查中改名为 `would_persist/would_not_persist/would_persist_empty`。row_key 只是候选身份，不能作为已存行的凭证。预检查会占用数据库行锁并读取文件，成本接近导入；只是当时状态，不保证后续可写，正式导入重新验证。

正式导入保留旧 counts/complete/results/errors/HTTP200，新增 `outcome=all_success|partial_success|all_rejected`、`summary`。全部成功包含重复项；已保存空值行也计为 persisted。summary.values 只统计已经执行行内的字段，整行拒绝不虚构字段执行；pending 与 conflict 可能已经持久化，不能当未执行或可自动覆盖。`write_outcome` 保留 pending 之前的真实写入/重复/冲突结果。errors 的 row_index 和 field_index 是原请求的零基索引，另带 canonical row_key/row_id（整行拒绝时 canonical 未知）。

`recovery` 仅为建议，没有自动执行/retry API。对于身份确定、无冲突、被拒绝且当前仍未存在的字段，可返回 `row_template`：保留当前 canonical row_key、完整身份/condition 和原失败字段；不重写成功值和 properties，不猜原文/数值/单位/来源。必须先读回当前行/来源/冲突，再依据原文明确修正模板中的失败内容、预检查，以新 request_id 提交。维度在原 values/properties 声明时，模板以完整 condition 保留，不能裁掉。pending 维度、冲突、整行拒绝、重复失败字段或字段已被同批后项写入时返回 blocked 与具体原因，按 steps 人工核实，不能用换 key/重试覆盖。

未知写结果：保留原请求，**同一请求体 + 同一 request_id** 安全重试查原回执（重放不是当前状态）。已确认失败的纠正：读回并只补失败项，**新 request_id + 稳定 canonical row_key**；改体复用旧 ID 仍拒绝。预检查不消耗 ID，亦不是正式回执；与正式导入之间状态变化必须重新校验和处理冲突。


前端未知结果保护（2026-10-18 补充）：已有未知提交查回时，401/403/409/422 等拒绝只说明本次查回未成功，不能证明之前未写入；此时仍阻止改体/新 ID。只有收到与原请求 ID、论文、行数和逐行结果相匹配的完整审计回执才解除保护；2xx 但 JSON 解析失败、缺少 complete/results 或回执身份/计数不符，一律继续标未知。初次请求明确的事务前 4xx 拒绝按已知拒绝处理。`unknownImportBody` **仅为当前页面内存保护**，不提供刷新或跨会话持久锁；原请求完整文本和 request_id 必须由用户/AI 自行保存，不能依赖刷新后保护仍在。保留原体原 ID 查回规则，不新增凭据存储或服务端恢复状态。


### 原文对象/材料条件覆盖计划（2026-10-18；实际可用版本以发布回执为准）

本功能只声明预期对象与具体结果对应，不调度步骤、不启用队列或正式科学验收，不修改已有科学输出。原始完整表格和科研性质数据行是不同计划项，章节已读不能替代图像处理。不得从计数、关键词搜索、read-cache库存、已有结果或模型一句“完成”反推完整原文清单；仅声明原PDF明确存在的材料/构型/位点/条件组合，不造笛卡尔积。

`GET /api/rebuild/papers/{paper_id}/coverage-plan?limit=50` 正常会话只读，按稳定item_key排序。`summary`总计始终来自全清单；items分页，跟随next_page直到has_more=false，每页固定revision（包含plan版本和当前output_revision）。计划/输出变化续页409，从头读。无计划旧文献legacy_without_plan=true、scope未知，不会自动回填。quality增加覆盖计划缺项/范围未知/结果核对过期；quality续页同时传expected_revision（科学输出）和expected_coverage_revision（quality_revision，含计划版本）。compact工作包仅公布有界端点和producer_contract.coverage_plan，不重复塞完整清单。

`POST .../coverage-plan/preview` 和 `PUT .../coverage-plan` 请求相同，均需会话+Origin+CSRF及有限JSON。preview只读不占ID/不写审计；PUT仅追加extraction_coverage_plan_saved专用AuditLog事件。全文替换声明，旧事件保留：`request_id`、`expected_plan_revision`（初次null）、`expected_output_revision`、`files`、`items`。同ID同体同演员重放原回执；改体/演员、旧CAS或旧output修订409且不写。原请求/演员/哈希与计划版本均审计，无DDL/WorkflowJob/科学实体写入。计划metadata不进入科学output_revision，避免自失效；结果对应核对另外绑定checked_output_revision。后续文件/输出/条件变化使旧对应stale/unknown，必须按当前版本原文重核后显式保存，不能沿用“已提取”冒充accepted。

files每项为`file_id, source_sha256, reading_pdf_sha256, declared_pages, scope_note`。哈希/物理页从现有工作包/file_inventory核实；PDF阅读页用原PDF，DOCX只接受已存在阅读PDF，不生成预览。未列文件/未声明页和未登记SI保持未知。item引用这些文件声明继承原始/阅读SHA，不重复制原文库存。

items每项：`item_key,title,kind,source,identity,condition,field_name?,label?,subfigure_label?,expected_columns?,origin,state,reason?,link?`。kind为row/value/table/figure/subfigure/section。source为`file_id,pages,locator,quote?,table_row?,table_column?`，跨页表只能同文件多物理页。identity仅声明明确的行身份字段（material等），condition是明确条件的子集；只作原样比较，不猜同义词。value须field_name；row/value须material。稳定item_key不能换成不同材料/条件/文件/种类，另一个对象用新key，历史变更可审计。

state为pending/extracted/not_reported/unknown/not_applicable。所有非extracted项必须reason=`{code,detail,checked_pages?,method?}`；not_reported还必须有覆盖本对象source.pages的checked_pages与核查方法method，不要求原文有“不报告”的句子；不能因无链接自动转成not_reported。未知不填0，不适用写真实理由。origin为reported/estimated/derived/image_pending，图像待处理不能报extracted。

extracted必须精确link：value需row_id/value_id/source_id，可带canonical row_key；row需row_id（有匹配本文件页的已存科学值与来源）；table/figure/subfigure需asset_id，可带asset_key；section需section_id及可核对的章节导入来源。程序检查同paper/file/pages、值所属行/field、明确material/condition与表坐标、资产类型/标签。table复用正式表表示及来源核验，声明expected_columns时须结构化列完全一致；图/子图须实际图像和解读，材料条件特定图的material_mapping须原样匹配。section只能证明章节来源，不能作为材料条件性质值的替代。不能重复借同一个值/图等填满不同项。错误extracted预检valid=false，PUT422拒绝整份metadata；不会自动修输出。

`declared_items_accounted_for`只表示这份明确声明中的项目已对应或已有未报告/不适用说明；始终scientific_acceptance=not_implied。页范围声明齐全也不能证明源对象全量或科学正确。旧文章不自动生成清单；保存计划不等于科学验收通过。

覆盖计划续页offset>0必须传expected_revision；已有计划时quality续页还必须传expected_coverage_revision，缺少返回400，旧pin返回409。计划GET同时返回原files声明、当前核查scope与declaration审计信息；原请求指纹保留实际提供字段（默认字段显式补入也视为改体），规范JSON哈希不对任意condition键作脱敏后再计算，避免不同请求哈希碰撞。输出修订变化使旧extracted/not_reported/not_applicable核对过期；pending仍待处理。row/value的reported/estimated/derived与实际值类型分别核对，图表/章节本体的extracted仅支持reported；图像数值估读请作为单独value项声明，不以图片存在代替估读事实。

覆盖计划缺项不会在本阶段自动改写或撤销既有正式科学验收回执；两套状态分离，不能把计划对应通过当作accepted，也不能借既有accepted掩盖新清单中的具体未处理项。


第二阶段初审补正：清单“只读查看对应结果”使用 `GET .../coverage-plan/items/{item_key}/output?expected_revision={清单revision}`，正常会话、只读事务，按计划中的精确row/value/source/asset/section ID定点取对象，不依赖数据表首页、不无限翻页、不打开身份编辑。详情返回当前实际ID、原值/表正文/章节原文与来源；清单/输出旧pin为409，缺对象404。该接口复用既有序列化和核对，只读详情不执行任何科学修改。

row项只表示在声明来源文件、全部声明物理页及origin范围内有至少一个合法匹配事实，不声称整行性质齐全；与value项共用原值/数值/来源规则及未裁决冲突检查。冲突/非法数值不能证明row已对应，但另一个真正合法的匹配事实可以；具体字段仍应分别声明value项。稳定item_key约束跨全部历史版本有效：从新版本移除声明并不删除历史，也不释放key；相同身份可重新声明，换文件/材料/条件/种类等必须新key。历史核查一次批量投影身份字段，不按item逐次查询，不创建科学DDL或回填旧数据。

quality的 `file_extraction_coverage_unknown` 属于“正式验收范围声明”，与本阶段“原文清单范围声明”分开。新计划全页声明不清除此unknown，不创建或更改freeze/review；页面分别说明，不要求为了清除此提示重新提取。coverage_plan.summary只反映原文清单，formal acceptance逻辑/状态保持原样。

row/value的匹配来源还必须同时属于该value、该row和该paper；不能借parent row错配的来源证明row已对应。

## 分段工作包与范围检查记录（不自动执行）

`GET /api/rebuild/papers/{paper_id}/staged-work-package?stage=source_scope&limit=25` 需正常会话，REPEATABLE READ READ ONLY。阶段为来源 `source_scope`、清单 `coverage`、补项 `extract`、核对 `reconcile`、交接 `handoff`；是只读索引与建议，不是执行器、队列或科学验收。默认只返回相关合同引用、版本、有限条目和最近每阶段一条检查摘要。原文和完整结果由已有修订绑定端点读取，索引不会把截断原文冒充证据。`extract&view=receipts` 分页读取正式导入回执头；要读写入合同可分别用 `view=rows_contract|table_contract|section_contract`，一次只返回已有的一类合同。章节继续仅由DS执行。

跟随 `next_page` 到 `has_more=false`；offset>0必须携 `expected_revision`。pin包含阶段/视图、来源/输出、计划generation+revision、科学导入/章节事件及回执、检查版本和同篇未知声明台账；旧pin或缺pin为409。从头重读，不混合版本。所有计数只说明所列范围，不证明源对象全量。来源阶段仍复用既有输出快照，因此不宣称数据库计算只读文件索引或生产性能提升。

`GET .../extraction-checks` 返回当前binding、check_revision和最近每阶段检查；`GET .../extraction-checks/{record_id}` 定点读取完整原声明、绑定、原assessment及当前freshness。没有检查记录的旧文献仍可使用原API，未知状态不否定既有科学结论。任何历史assessment均标为 `assessment_as_recorded`，先看freshness；stale不能沿用，原请求重放也不是当前状态证明。

`POST .../extraction-checks/preview` 与 `PUT .../extraction-checks` 同一请求，均需正常会话+Origin+CSRF及有限JSON。preview为只读，不占ID。PUT仅追加一条 `staged_extraction_check_recorded` AuditLog；无表迁移，无WorkflowJob/科学实体/状态/正式验收修改。Paper锁下CAS绑定当前来源、输出、计划和真实回执；`expected_check_revision`还防检查记录并发覆盖。同ID/原提供字段/actor原样重放原回执，异体或异actor409；伪造actor为422。

请求结构（值从上述GET读取，不编造SHA）：

```json
{
  "request_id":"persist-before-send-unique-id",
  "stage":"reconcile",
  "expected_check_revision":null,
  "expected_binding":{
    "output_revision":"64hex", "source_revision":"64hex", "receipt_revision":"64hex",
    "plan_revision":null, "plan_generation":null, "known_unknown_revision":"64hex"
  },
  "source_scope":[{"file_id":"uuid","source_sha256":"64hex","reading_pdf_sha256":"64hex","pages":[1,2],"locator":"实际核对范围"}],
  "item_keys":["实际计划item_key"], "receipt_ids":["实际本篇导入回执uuid"],
  "unknown_requests":[], "review_statement":"实际原文核查范围和仍有的限制"
}
```

跨篇文件/回执、错误SHA/页/清单key、空白声明拒绝且不写。合法但范围不全可以保存为checkpoint，不能得到可交接建议。检查声明的原PDF科学语义始终 `not_independently_verified`，即使程序关联检查无阻碍，也只表示当前明确范围可携交接复核，始终 `scientific_acceptance=not_implied`。GET还要求同actor当前有效的reconcile/handoff明确范围记录，不能用空问题列表自动得出交接建议。正式验收范围unknown单独显示，不要求为此启动freeze/review。

真实证明取自同篇正式receipt及其event_ids，并把事件after与当前row/value/source/asset逐字段比对；旧成功回执不能证明后来输出。章节复用既有不可变来源事件与回执校验。历史无对应审计/不支持的旧修正保留待核实，不补造审计、不否定原结论。partial需读回实际失败项，字段级失败仅在后来成功回执证明当前合法值时解除；不能定位的整行/资产拒绝、身份或冲突仍保守待核实，本功能不新增恢复执行。

未知请求须保存原operation、request_id、原 `run_audited` 指纹、reason。指纹是原已验证model的 `model_dump(mode='json',exclude_unset=True)` 加当时task_id形成 `{body,task_id}`，使用原服务相同json.dumps(sort_keys=True,ensure_ascii=False,allow_nan=False)默认分隔符后SHA256，不能拿元数据请求哈希替代。**原体必须先持久化**；无法重建原指纹时保留待人工核实，不另造新请求冒充恢复。台账按原actor/operation/id/指纹保存；不能省略或换账号隐去同篇未知，只由匹配真实回执确认结果，partial仍单独核对。不把HTTP200、模型一句完成、计数或空errors视为全部成功。

回执头还给出原complete、errors_total及只读errors_endpoint。`GET .../extraction-import-receipts/{receipt_id}?limit=25` 只读读取同篇正式回执的原失败项；每页最多100项，续页必须携本回执revision，缺/旧pin为409。原outcome/complete均显式标为as_recorded，不声称当前恢复。无失败项不代表全篇完成。详情页复用已有只读弹窗查看失败项及续页，无写表单。

## 历史尝试与历史验收索引（受控承接）

`GET /api/rebuild/papers/{paper_id}/history-reconciliation?view=attempts|reports&limit=25` 正常会话、RR READ ONLY，不访问原生/WorkBuddy后端、不恢复或停止线程。原WorkflowJob及状态保留；稳定job_id/run_id/attempt_token/thread_id/turn_id分别显示，latest_recorded只指最新库内记录。旧running、超时、有结果、后来的completed/历史accepted均不证明当前执行或安全停止。原completed/failed只称ended_as_recorded；现存完整verified_stop回执匹配原身份/终态/goal和时间时只称stopped_as_observed（历史观测，本轮未回查）。缺字段、不可读或不曾回查保持当前未知。`can_unlock=false/occupancy_changed=false`固定，不影响原队列/锁/原生授权。

报告候选来自服务端固定`settings.storage_root.parent/history_evidence/catalog.json`，缺失时明确catalog_unavailable_or_invalid，无自动扫描/导入台账。客户端不能给路径、上传报告或传可信/accepted/native状态。此目录/文件须由总指挥逐篇核准安排；当前代码任务不在生产创建该目录、不保存生产登记。目录及所有组件不能symlink；固定相对普通文件、无../绝对/隐藏/反斜杠路径、大小上限、完整FD/字节hash核验，API只返回白名单索引/范围/哈希，不返回任意原文件正文/绝对路径。

catalog格式为`{format:"history_catalog_v1",entries:[{report_id,paper_id,evidence:{path,sha256}}]}`。每条evidence文件为`history_evidence_v1`结构化封套：paper_id、original:{path,sha256}真实原报告、historical_verdict、observed_output_revision|null、source_scope:[{file_id,role,source_sha256,reading_pdf_sha256,pages,locator}]、limitations、attempt_links。未知版本/范围不能由当前计数补齐；封套由服务端核准目录提供，不是客户端自报可信状态。结构化原报告若显式paper_id/verdict/observed_output_revision与封套冲突则拒绝；原文件实际存在及hash一致也只验证文件字节，不独立验证科学含义。

报告始终区分historical_verdict_as_reported与current_acceptance=not_granted；版本为version_unknown/stale/current_version_match_only/current_version_scope_unknown，范围unknown/stale/partial_declared/full_declared。全页声明仍不证明对象穷尽，旧报告accepted不自动变成当前有效accepted。正式独立review/freeze/同账号grant仍延后、不在本接口激活或绕过。没有新登记不否定已有图表、章节或科学结论。

POST同路径`/preview`和PUT同路径，均会话+Origin+CSRF/有限JSON；预检只读不占ID。body仅`request_id,expected_revision,expected_binding,report_id,expected_report_sha256,kind,note`；expected_binding复用stage checks，expected_revision来自本历史索引。kind为report_index或attempt_supersession；后者还给old_job_id/new_job_id，必须同篇稳定身份、new记录较新，且受控报告attempt_links精确包含二者job/run/attempt身份。只形成工作选择的替代声明，不证明原生停止，也不解除占用。一般人工note不能作为native证明。

PUT仅追加history_reconciliation_recorded专用AuditLog，保留原WorkflowJob/回执/科学表；没有新表或迁移。Paper锁下CAS；同ID原体同actor重放原回执，异体/actor或版本冲突409；非法引用422。重放回执是as_recorded，不代表当前freshness。`GET .../records/{record_id}`定点读原登记/绑定及当前freshness。来源、科学输出、计划、具体报告证据或引用job版本变更使相应登记stale；metadata自身不进入科学revision。历史列表分页有界、续页必须携expected_revision，缺/旧pin409，不混合版本。

旧失败核对复用原mutation receipt/event及value.conflict.correction_requests/source_corrections；不接受客户端proof。缺row_id时只从原回执results或同item_index真实事件取唯一canonical row；字段、原身份、同篇来源、原alias唯一性、时间和当前合法输出均须成立。重复alias、身份漂移、跨篇/断链/先后不明/缺审计仍待核实。当前值原字符/分析数值、精度、单位、missing/type及原来源校验不放宽。值CAS要求能从真实导入事件独立重算expected_revision并重建原请求指纹；原协议缺少多次CAS之间完整时间/冲突快照时不猜历史版本。来源CAS可核对完整before/after、连续expected_revision、原指纹和当前实体，也支持有合法值CAS锚点的新来源随后更正。原回执保留；识别恢复仅表示当前字段与后续真实写入证据对应，不等于科学验收。尚无法对应的旧失败继续待人工核实，不改科学值或靠计数清空。


历史原报告字段合同 `report_level_aliases_v1`：只识别顶层 `paper_id`、`paper.paper_id`、`paper.id` 这三个稳定UUID身份路径，以及顶层 `observed_output_revision/output_revision/revision` 科学修订别名。存在多个身份或版本时须互相一致且与本篇/封套一致；矛盾拒绝。顶层 `scientific_verdict/extraction_verdict/verdict/decision` 为支持的整篇结论路径，仅有限词表按大小写归族（accepted/PASS、accepted_partial、PASS_WITH_LIMITATIONS、partial、needs_fix、needs_review、failed/rejected等分开保留）。多个字段/封套结论须同族，partial/limited不能改叫accepted。返回的historical_verdict_as_reported为原报告实际原值，不是封套替换值。

`status` 只有明确format/record_type/report_type等于 `scientific_review_v1|article_acceptance_v1|independent_scientific_review` 才可作为支持的整篇科学结论。无该标识的status=partial等仅约束保守冲突，不可单独证明科学结论；running/queued/completed/done/success等一般任务状态绝不推导科学通过。不泛搜嵌套checks/results/fresh_db_readback/子项的通过或版本来确定整篇。真实已核形状ROOT_ARTICLE_ACCEPTANCE使用paper_id/revision/extraction_verdict；B050类REVIEW使用paper.paper_id/verdict但缺科学revision时仍version_unknown，不能从其子项读回填补。

缺原报告稳定身份、未知结构/结论、数组、MD/TXT等只保留 `semantic_basis=operator_declaration_only`。字节/hash仍可核对并登记索引，但原报告科学结论与版本不由封套补齐：`historical_verdict_as_reported=null/historical_output_revision=null/comparison=version_unknown`，封套值仅放 `operator_declared_verdict/operator_declared_output_revision`；页面明确“仅操作者声明，原报告语义未机器确认”。支持身份/结论但缺原版本时仍version_unknown。source_scope的basis始终是操作者范围声明与当前文件身份核对，不声称已解析原报告范围或证明科学正确。

旧错误恢复还要求全部已提供row_id、row_index、row_key与原结果/同项事件及该行有证据的历史/当前别名一致；不得通过row_id交集丢弃矛盾候选，或以别名全局唯一替代“属于同一行”。旧results无显式row_index时，仅原one-result-per-submitted-row计数合同允许位置索引，或用原同项事件/明确身份；缺依据、错误index或跨行key继续待核实。有原审计锚点的合法旧alias仍可对应，不凭当前同名猜行。


### 候选合同：来源历史化与章节顺序（2026-10-19，尚未发布）

`POST /api/rebuild/sources/{source_id}/lifecycle` 使用现有工作台会话、同源及 CSRF 检查。
请求包含 `paper_id`、GET 来源返回的 `expected_revision`（`source_revision`）、稳定 UUID `request_id`、
`action: "deactivate"` 与非空 `reason`。CAS 冲突、同 ID 改 body/actor 返回 409；同 actor/body 重试只读回。
不删除或改写来源、原 quote 或科学值；审计追加在 value.conflict.source_lifecycle。值/行/工作包的
`sources` 仅含当前证据，`historical_sources` 保留停用证据，`historical_candidates` 保留仅关联停用来源的候选读回；原始审计及候选关联仍保留在库中。
失去最后证据是 `evidence_status: "no_current_sources"`，不把科学值改成 missing。
停用首选来源仅清空指针，不猜测其他候选；正确首选来源不受其他来源停用影响。
无恢复入口；普通纠正、导入与去重不可静默复活停用来源。

`POST /api/rebuild/papers/{paper_id}/sections/order` 同样要求工作台会话、同源及 CSRF。
请求：UUID `request_id`、新鲜 `expected_revision`（章节返回的 `section_order_revision`）、非空 `reason`、
按原文顺序排列的 `sections`。每项为 `section_id`、`content_sha256`（provenance 中原导入哈希）、
`sources: [{page_number, locator, quote}]`，页必须在该章原物理页范围内。
必须覆盖该篇所有有来源的当前章节一次；不同 file/page_basis/reading hash 分组必须连续，同组原物理起始页不能倒退。
未绑定文件的 legacy 章节保留为未知，不得为排序伪造文件身份；边界纠正历史独立保留。
声明追加 AuditLog，不改原文章节、页码、层级或来源。支持同 body/actor 幂等、更新后 CAS 失效；
原章节或纠正状态变化使旧声明失效。无显式声明时，同文件同页明确十进制原章号自然排序；
混合/无编号章仅在校验阅读件 SHA256 且找到唯一逐行原题时按阅读件文字位置排序。
无法证明的旧数据按页与内容稳定显示并标记 `page_then_content_order_unverified`，不依赖 UUID、
不臆测罗马/字母/无编号科学章序。它们可通过原文证据声明显式顺序。展示不再附加列表索引章号。
科学独立验收、正式 accepted/gates/freeze/coverage-plan 合同不在本候选修改范围内。
