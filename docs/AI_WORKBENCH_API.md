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
