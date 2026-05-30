# LitAI 稳定升级路线：DFT 数据库、机器学习数据集与论文写作辅助

## 目标排序

第一优先级是把检索到的文献稳定转化为可信 DFT 数据库，并能导出给机器学习使用。系统必须保留证据链、人工确认状态和原文出处，不能把未经确认的 AI 候选当作可训练事实。

第二优先级是把可信文献证据接入论文写作流程，先做“候选引用建议”和“引用草稿”，后续再对接 Zotero 等文献管理系统。写作侧只能辅助定位与草拟，不能自动生成最终引用、不能自动确认引用。

## 当前基线判断

项目已经具备文献库、解析任务、结构化提取、人工 review、安全导出门、证据定位、AI 写作、写作引用辅助等基础能力。下一阶段不应扩成泛用 RAG 平台，而应围绕“可信结构化科学数据闭环”继续加固。

当前最应补强的是三个闭环：

- 文献检索与入库：让论文来源、解析状态、失败原因、重试路径更清晰。
- DFT 抽取与复核：让 DFT result、catalyst sample、DFT setting、evidence locator 之间关系更稳定。
- 数据集导出与写作使用：导出机器学习可用数据，同时保持 safe_verified 安全边界；写作只消费可信证据，不反向污染数据库。

## 不变的安全边界

- 不把 AI 候选直接写成 verified。
- 不把未通过证据链和人工确认的数据导出为 ML 训练事实。
- 不自动生成 bibliography、final citation 或 confirmed citation。
- 不让写作功能写入 papers、reviews、evidence_locators 等核心事实表。
- 不改 active DB，不绕过现有 review safety gate。

## 阶段 0：稳定基座与验收体验

目标是让已有功能“看得懂、用得稳、失败可解释”。

- 保持全站中文化、顶部导航 sticky、详情页空状态、PDF/证据定位失败提示清晰。
- 修复 AI 配置提示：明确区分 Writer LLM、Embedding、内部解析配置，不让“未配置”和“解析中”同时误导用户。
- 保持文献库、AI 写作、写作辅助、论文详情、DFT 数据库布局一致，避免面板溢出。
- 给关键按钮补状态反馈：请求中、成功、失败、无可导出数据、被安全门阻止。

验收标准：

- `npm test -- --project=chromium` 通过。
- 写作辅助、论文详情、DFT 数据库核心 smoke test 通过。
- 用户能明确知道“为什么解析不了、为什么不能导出、下一步该做什么”。

## 阶段 1：DFT 机器学习数据集导出

目标是让 DFT 数据可以被 ML pipeline 稳定消费，但不扩大事实可信范围。

功能清单：

- 新增只读 API：导出 ML-ready DFT dataset，包含 metadata、filters、safety gate summary、records。
- records 结构包含 paper、target、catalyst、dft_settings、provenance 五块。
- 只导出通过 safe_verified + required evidence + exact locator 的记录。
- 保留 blocked_count 与 blocked_reasons，方便判断数据集不足是因为缺 review、缺证据还是缺定位。
- 前端 DFT 数据库页增加“导出 ML JSON”按钮，不改变现有 CSV 导出。

验收标准：

- 缺 review、缺 evidence、缺 evidence_text、缺 locator 的记录不会进入 records。
- 导出 JSON 可直接被 Python/Notebook 读取。
- 现有 CSV 导出测试不回退。

## 阶段 2：DFT 数据质量面板

目标是让“为什么数据不能训练”可视化。

功能清单：

- 在 DFT 数据库页展示数据质量概览：可导出数、被阻止数、主要阻止原因。
- 增加按阻止原因筛选：missing_review、missing_evidence、missing_evidence_text、unsafe_locator。
- 从质量面板跳转到对应论文详情或 review workbench。
- 对同一论文的 DFT setting、catalyst sample、DFT result 做完整度提示。

验收标准：

- 用户能从 DFT 页面定位需要补 review 或证据定位的文献。
- 不新增自动 verified，不替用户确认事实。

## 阶段 3：文献检索与入库稳定化

目标是提高文献进入数据库的质量，而不是单纯堆数量。

功能清单：

- 增加检索任务中心：query、来源、时间、导入数量、失败数量、失败原因。
- DOI 去重和重复合并提示更明确。
- 解析失败支持重试，并保留失败日志摘要。
- 对 PDF 不可预览、无 DOI、TEI/Docling 解析失败等情况给出中文解释。

验收标准：

- 批量导入后用户能判断哪些文献可解析、哪些需要人工处理。
- 重试不会重复写入同一篇论文。

## 阶段 4：写作引用辅助增强

目标是让论文写作更快找到证据，但不越过“候选建议”边界。

功能清单：

- 写作上下文与候选结果做分栏/目录化布局，支持拉伸和隐藏。
- 候选卡片展示证据片段、来源论文、页码/章节、可信状态。
- 引用草稿只输出 proposal，不出现 Insert Citation、Final Citation、Generate Bibliography 等危险文案。
- 预留 Zotero adapter 接口设计，但第一阶段只做导出 CSL JSON/BibTeX 草稿，不自动插入。

验收标准：

- confirmed citation 仍只能来自 API 明确允许的 confirmed/safe_verified 状态。
- 写作页面不写入核心事实表。

## 阶段 5：面向 ML 的数据版本与审计

目标是让训练集可复现。

功能清单：

- 数据集导出 metadata 增加 schema_version、export_time、filters、record_count、blocked_reasons。
- 后续可增加 dataset manifest 文件，记录导出参数和 git commit。
- 增加字段级 schema 文档：目标值、单位、吸附物、反应步骤、催化剂结构、DFT 设置、证据链。
- 增加单元归一化建议，但默认不覆盖原始值。

验收标准：

- 任一导出文件能追溯到来源论文、字段、证据和安全门状态。
- 单位归一化只作为派生字段，不删除原始字段。

## 暂不做

- 不做泛用 Agent 市场。
- 不做自动论文全文生成。
- 不做自动最终参考文献生成。
- 不做未经确认数据的大规模 ML 导出。
- 不做复杂 RBAC，除非将来进入多人协作环境。
- 不做把 AI 写作和写作引用辅助合并成单页大工作台，避免破坏现有验收路径。

## 当前推进顺序

1. 落地 ML-ready DFT JSON 导出，复用现有安全门。
2. 在 DFT 数据库页增加导出入口和状态反馈。
3. 增加后端安全门测试与前端 smoke 测试。
4. 再推进 DFT 数据质量面板。
5. 最后增强写作引用辅助与 Zotero 预留接口。
