# LitAI 提取与审核协议 v0.2

## 文档定位

本文是 `literature-ai` 当前阶段的执行协议，用于统一文献入库、PDF 解析、元数据补齐、结构化候选抽取、证据追溯、人工审核、写作引用和前端展示行为。

本协议吸收 [TiAl Data / Extraction Protocol](http://101.42.36.213:3391/#properties) 的优点：覆盖率优先、协议显性化、状态流锁定、字段可追溯、图像只作有边界的证据来源。但本项目不照搬其“人工逐篇录入员”模式；`literature-ai` 的定位仍是 Codex 使用的本地文献工具台。

## 核心原则

1. 自动解析、AI 抽取和外部模型结果默认都是候选，不是最终事实。
2. 每个正式字段必须能追溯到原文、图、表、元数据来源或人工确认。
3. 文献元数据完整不等于证据安全，也不等于抽取结果 verified。
4. 前端页面是工作台和投影层，不是数据真源。
5. 结构化数据、长文本、图像、证据 locator、审核记录必须分层保存。
6. Codex 可以补齐候选和提出修复，但不能替代用户做最终人工确认。

## 文献元数据协议

### 必需字段

文献入库和引用候选至少追踪以下字段：

- `title`
- `authors`
- `journal`
- `year`
- `doi`
- `impact_factor`

其中 `title/authors/journal/year/doi` 存在于 `papers` 表，`impact_factor` 存在于 `paper_impact_metadata` 表。`volume/issue/pages/publisher` 当前尚未建模，诊断报告只能把它们列为“当前未建模字段”，不得把它们计入缺失率。

### 补齐规则

- DOI、年份、期刊、标题、作者优先来自 DOI/URL provider metadata，例如 Crossref/OpenAlex；PDF 解析只作为 fallback。
- DOI 必须规范化为小写、去掉 `https://doi.org/`、`doi:`、末尾标点。
- 如果新 DOI 与已有 DOI 冲突，必须阻止自动合并，并进入人工确认。
- 年份必须是可解释的四位年份；异常年份不应硬写入。
- 影响因子只能通过可信 CSV/JSON 手动导入，按规范化期刊名匹配；系统诊断端点不得联网抓取影响因子。
- 补齐元数据不得自动提升 `workflow_status`、`reviewer_status`、citation eligibility 或 ML export gate。

### 诊断输出

元数据诊断至少应返回：

- 总文献数
- 完整文献数
- 缺元数据文献数
- 每个必需字段的覆盖率
- 每篇缺失文献的缺失字段、建议动作和安全免责声明
- 影响因子导入模板
- 当前未建模字段说明

## PDF 解析协议

### 解析顺序

1. 保存 PDF 到标准 storage。
2. 运行 PDF 质量检查，记录 `pdf_quality_status`、`pdf_quality_score`、`pdf_quality_report`。
3. 用 GROBID 提取 TEI、标题、摘要、作者、DOI、年份、期刊、参考文献和正文结构。
4. 用 Docling 提取 Markdown、页面文本、表格、图片候选和版面 provenance。
5. 如果 GROBID 标题是文件名或缺失，允许从 Docling 首页文本推断标题。
6. 如果 GROBID 缺 DOI，允许从 Markdown 前部、citation 行或 DOI URL 中提取 DOI。
7. 如 provider metadata 可用，优先用 provider 补齐缺失元数据，但不得覆盖明确冲突的 DOI。

### 质量阻断

如果 PDF 质量报告要求人工确认：

- 文献可以保留元数据和 PDF。
- 不应写入正文 sections、tables、figures 或结构化候选。
- 工作流状态应提示需要人工确认。
- 详情页必须明确显示“尚未可安全解析”或类似提示。

## 结构化候选协议

### 候选粒度

一篇论文可以产生多条记录。拆分原则：

- DFT 结果按材料、构型、性质、条件、吸附物或反应步骤拆分。
- 实验性能按材料、工艺、测试条件和性能组合拆分。
- 图表按图号、表号或可定位的图表对象独立记录。
- 写作卡和机理候选按研究空白、方法、机制、结论、写作逻辑拆分。

### 字段对象

非身份字段建议使用对象或对象数组表达，不推荐裸值：

```json
{
  "value_original": "-1.23 eV",
  "unit_original": "eV",
  "value_normalized": -1.23,
  "unit_normalized": "eV",
  "condition": "Li adsorption on vacancy defect",
  "text_source": {
    "page": 4,
    "section": "Computational Results",
    "excerpt": "The adsorption energy is -1.23 eV."
  },
  "image_source": null,
  "confidence": 0.82,
  "review_status": "candidate",
  "normalization_note": "No unit conversion required."
}
```

### 禁止项

- 不得从曲线图、散点图、热图或扫描表中硬猜数值。
- 不得把没有 source 的 AI 输出写成正式事实。
- 不得把图像 OCR 或 VLM 结果直接标记为 verified。
- 不得因为元数据完整就默认该论文适合引用或导出。

## 图像和表格协议

图像分三类：

- `data_figure`：曲线、柱状图、散点图、表格截图等，可能用于后续人工数字化。
- `knowledge_figure`：机理图、结构图、流程图、表征图，适合阅读和写作理解。
- `invalid_crop`：出版社标志、CrossMark、页眉页脚、孤立图标、裁剪噪声。

图像入库规则：

- 必须保留 caption、page、image_path、provenance。
- 无 caption 或疑似装饰图应过滤或标记为噪声。
- 只在图中文字或正文明确给出数值时记录数值；纯视觉估读进入 `figure_pending` 或 `needs_digitization`。
- 表格应保留 markdown_content、caption、page、provenance，并可作为证据 chunk。

## 审核状态流

推荐状态流：

`Imported -> Quality_Checked -> Parsed_Material_Ready -> Codex_Candidate -> External_Reviewed -> Human_Confirmed -> ML_Ready / Citation_Ready`

状态含义：

- `Imported`：文献已入库，可能只有元数据。
- `Quality_Checked`：PDF 质量已评估。
- `Parsed_Material_Ready`：PDF 文本、图表和候选材料已生成。
- `Codex_Candidate`：Codex 或规则生成候选，仍需核对。
- `External_Reviewed`：外部模型或人工辅助审阅给出意见，但不等于最终确认。
- `Human_Confirmed`：用户完成最终确认。
- `ML_Ready / Citation_Ready`：满足对应导出或写作引用安全门。

任何自动流程都不得直接把候选升级为 `Human_Confirmed`。

## 交付物

每篇完成解析的文献至少应有：

- `metadata.json` 或数据库中的完整 paper 元数据投影
- PDF 质量报告
- TEI / Markdown / Docling JSON 中间产物
- sections、tables、figures
- structured candidates
- evidence locators
- audit log
- metadata diagnostics 状态
- Codex context bundle

## 前端工作台要求

前端应显式展示：

- DOI、年份、期刊、影响因子是否缺失
- PDF 是否存在、是否 metadata-only
- PDF 质量状态
- 解析产物数量
- 候选是否 verified 或仍为 candidate
- 影响因子来源和年份
- 元数据诊断覆盖率
- 协议和安全护栏说明

前端不得把缺失字段隐藏成 `-` 后不再提示；应使用“年份待补、期刊待补、DOI待补、IF待补”等明确语言。

## 数据安全边界

默认禁止以下动作：

- 未经确认执行 migration apply
- 未经确认对真实 active SQLite 做批量 extraction apply
- 删除真实 PDF、解析产物、artifacts 或 registry
- 自动联网抓取影响因子并写库
- 自动把 candidate 标记为 verified
- 自动解锁 Citation/ML 导出

允许的安全动作：

- 只读诊断
- 生成补齐建议
- 生成 CSV/JSON 导入模板
- 写入代码、测试和文档
- 在用户明确授权后导入可信影响因子表

## 值得持续借鉴的 TiAl Data 做法

- 首页就给出 DOI coverage、text-readable、year range 等摘要指标。
- 抽取协议作为产品页的一部分，不藏在开发文档里。
- 数据可视化围绕字段覆盖率、版本、置信度、分布和相关性展开。
- 图像记录区分 metadata-only、extracted image、source PDF 链接。
- 记录级数据保留 schema_version、record_status、confidence 和 source location。

本项目的对应实现方向是：元数据诊断覆盖率前置、文献卡片显式显示待补字段、影响因子通过可信导入补齐、结构化候选始终保留证据和审核状态。
