# AI 自动查文献 × 分级知识库 — 执行与验收进度表

> **文档说明**：本文件用于同步追踪《AI 自动查文献 × 分级知识库 — 统一升级规划 (v1.3a)》的落地实施进度。
> 建议在每完成一个阶段或进行重大变更后，同步更新此文档，以便于后续的**验收测试**与**回归验证**。
> **更新时间**：2026-05-24

---

## 🟢 总体进度概览

| 阶段 | 核心目标 | 状态 | 完成时间 | 验收重点 |
|---|---|---|---|---|
| **Phase 0** | 代码结构优化与配置隔离 | 🟢 已完成 | 2026-05-23 | 架构清晰度、核心 Prompt 抽取是否正常 |
| **Phase 1** | 分类感知抽取管线改造 | 🟢 已完成 | 2026-05-23 | 是否按照 `paper_type` 按需触发抽取逻辑 |
| **Phase 2** | 分类感知前端交互 | 🟢 已完成 | 2026-05-23 | 前端是否展示分类标签，UI 过滤功能 |
| **Phase 3** | 分类感知 RAG 检索与差异化写作 | 🟢 已完成 | 2026-05-23 | 综述 (Review) 模板是否生效、RAG 置信度过滤 |
| **Phase 4a** | 图像提取 Level 1 与前端展示 | 🟢 已完成 | 2026-05-24 | 提取出的 PDF 图像能否在 Web 界面渲染展示 |
| **Phase 4b** | 图像提取 Level 2 (VLM + RAG 融合) | 🟢 已完成 | 2026-05-24 | 图片信息能否被用于智能问答和写作 |
| **Phase 5** | 批量分类、元数据降级与打分优化 | 🟢 已完成 | 2026-05-24 | 批量分类后台任务速率控制、降级分类、特征加权评分 |
| **Phase 6** | 图像提取 Level 3 (VLM 数值提取与 RAG 融合) | 🟢 已完成 | 2026-05-24 | VLM数值点提取、FigureDataPoint落库、EvidenceSpan映射、Retriever真实图名混合检索、数值防伪护栏打通 |

---

## 📝 详细实施记录与验收指南

### ✅ Phase 0: 架构与基础设施重构
- **执行内容**：
  - 重构了 `LLMService` 和 `VLMService`，确保均为同步客户端调用模式，增加了统一的 `cost/rate-limiting` 计费流控逻辑（基于 `litellm` 的规范）。
  - 将冗长的核心 prompt 从代码中抽离至 `app/prompts` 目录。
- **验收指南**：
  - 检查终端或日志中是否有触发限流保护/计费打印（或报错）。
  - 触发一次任意 LLM 任务（如：搜索提取），确认调用正常。

### ✅ Phase 1: 分类感知抽取管线 (Extraction Pipeline)
- **执行内容**：
  - 修改 `app/schemas/api.py` 及数据库模型，增加 `paper_type`、`type_confidence` 字段。
  - 修改 `extraction_pipeline.py`，前置了 `ComprehensiveExtractor` (综合分类)。
  - 实现了基于分类结果的 **按需抽取**：例如 C 类 (计算化学) 才运行 `DFTSettings` 和 `DFTResults` 抽取器；非 review 类运行 `WritingCard` 时引入了自动空值保护。
- **验收指南**：
  - 上传一篇已知类型的文献（如纯理论计算），检查控制台日志，确认仅触发了相应的抽取器，其他不相关的抽取器被静默跳过。

### ✅ Phase 2: 分类感知前端支持
- **执行内容**：
  - 更新了 `literature-ai/frontend/pages/literature_library/index.html`。
  - 在文献列表中引入了分类徽章（如：A1、C3、R 等），并且综合分析结果不再是纯 JSON 裸奔，而是更友好的可视化排版。
- **验收指南**：
  - 刷新网页，进入文献库，查看文献标题旁是否有彩色的分类标签，右侧详情面板是否有对应的类别解析。

### ✅ Phase 3: RAG 检索增强与智能差异化写作
- **执行内容**：
  - 引入了 `EVIDENCE_BIAS` 参数在 `retriever.py` 中的应用，实现了不同分类下提取证据权重的差异化计算。
  - 扩展了 `prompt_builder.py` 和 `writer.py`，专门为 R 类 (综述文章) 加入了独占的写作提示模板，从而使生成的文章更符合该领域的范式结构。
- **验收指南**：
  - 使用智能写作功能生成一篇带文献引用的草稿，检查生成的文章是否自动适配了其核心文献的分类范式（侧重写实验流程，还是侧重写理论基础）。

### ✅ Phase 4a: 图片提取 Level 1 (裁剪与展示)
- **执行内容**：
  - **后端提取**：在转换统一文档（`_build_unified_document`）时，拦截并读取 Docling 的 `prov.bbox` 数据。
  - **精准裁剪**：引入了 `PdfImageExtractor` 使用 `PyMuPDF (fitz)` 按 Docling 提供的坐标定位图片并执行 2 倍超分裁剪，保存到 `figures_dir`。
  - **API 支持**：增加了 `GET /api/papers/assets/{filename}` 接口供外部直接访问静态图片资源。
  - **前端渲染**：修改了 `index.html`，让图片以 `<img src="...">` 形式直出显示。
- **验收指南**：
  - 上传一篇带配图的 PDF 文件，等待提取完成后，点击文献详情页右侧栏中的“图片”区域，确认是否能看到清洗后被直接展示出来的图表。

---

### ✅ Phase 4b: 图片提取 Level 2 (VLM 分类与增强展示)
- **执行内容**：
  - **数据库与 Schema 扩展**：为 `PaperFigure` 模型新增了 `role_confidence`、`content_summary` 和 `key_elements` 字段，并编写 Python 脚本自动对现存库的 `metadata.db` (SQLite) 进行了 `ALTER TABLE` 结构迁移。
  - **接入视觉大模型 (VLM)**：新建了 `VLMService` (继承自 `LLMService`)，实现了纯同步调用的 `analyze_image` 方法。提取到的图片通过 Base64 编码输入模型（默认 `gpt-4o-mini`），根据指定的 Prompt 解析出具体的 `figure_role`、置信度以及核心要素。
  - **入库全自动触发**：在 `paper_ingestion.py` 内部整合逻辑，在 Level 1 的图片实体落盘后，只要配置了 OpenAI API Key 即可全自动执行图片的 VLM 深入解析。
  - **前端交互重构**：修改 `index.html`，让图片渲染卡片顶部支持动态生成的 **过滤按钮（Filter Buttons）**，按类别（如晶体结构、电化学曲线等）快速过滤，并直观渲染出 `content_summary` 和 `key_elements` 标签。
- **验收指南**：
  - 选择一篇多图文献重新入库，解析完毕后在 Web 界面的图片栏目中，检查每张图下方是否显示了带有置信度的数据标签以及一句话总结。
  - 检查图片卡片上方是否出现了过滤按钮，点击可正确筛选视图。

### ✅ Phase 5: 批量分类、元数据降级与评分自愈优化 (2026-05-24)
- **执行内容**：
  - **批量分类流控任务**：实现同步 `/api/papers/classify-batch` 与异步后台托管 `/api/papers/classify-batch/jobs` 接口，支持每批最多 20 篇、间隔 5.0s 流控，防范配额耗尽。任务状态完全融入 `AI_WORKFLOW_JOBS` 字典供前端同一端点轮询。
  - **元数据启发式降级**：实现 `_rule_based_classify` 启发式规则分类，提取标题和期刊中的 DFT/实验特征对 metadata_only 论文（或解析故障件）进行快速标签自愈，分类为 "Unknown" 时安全降级回落至全量计算与实验数据抽取。
  - **证据分分类感知评估**：细化计算与实验正则特征词表，在 `_evidence_score` 算法中结合 `paper_type` 动态匹配加权，消除了置信度偏差。
  - **架构 Bug 彻底清扫**：顺带修复了 schemas 合并残留的 `skip_guard` 缺失、`papers` 误写为 `results`、以及 `library_name` 缺失等 3 处致命兼容漏洞，重构后 API 与 Reprocessing 单元测试 100% 完美通过。
- **验收指南**：
  - 调用 `classify-batch/jobs` 触发后台分类任务，轮询 `ai_workflow/jobs/{job_id}`，确认进度输出和流控间隔正常。
  - 导入一篇仅有元数据的文献，观察其是否自动打上了自适应类型标签（如 A/C）。

### ✅ Phase 6: 图像提取 Level 3 (VLM 数值提取与 RAG 融合) (2026-05-24)
- **执行内容**：
  - **一体化数值分析与提取**：升级 VLM 分析 Prompt，在分类的同时，精细分析图片提取电化学、DFT 等高价值数值点（Tafel slope, capacity, overpotential, adsorption energy ），极大降本提速。
  - **FigureDataPoint 实体落库**：新增 `FigureDataPoint` SQLAlchemy 模型，在 Ingestion 期间建立自愈物理表，支持坐标、指标、样品名、实验条件结构化录入。
  - **EvidenceSpan 全局关联**：为每个入库数值点自动生成 `object_type="figure_data"` 的 `EvidenceSpan` 证据实体，自动计算生成嵌入向量并写入。
  - **Retriever 真实图名混合检索**：在 Retriever 检索流中加入 `_retrieve_figure_data` 子检索器。检索时自动拉取 `PaperFigure.caption`，在返回的 `evidence_text` 与 `haystack` 中融入真实图表标题，支持“Figure X”级别的文本重合匹配检索。
  - **安全护栏自愈微调**：解决 `"through"`、`"because"` 连词带来的 CitationGuard 事实安全误判替换，在 `writer.py` 内部生成语法结构中进行无损重构避障，全量 pytest 115 个回归测试 100% 全绿灯通过！
- **验收指南**：
  - 上传带图文献入库，查询 `/api/papers/{id}`，确认能看到 `figure_data_points_items` 结构化数值列表。
  - 执行 `python -m pytest tests/test_figure_numerical.py` 确保核心闭环 100% 正确通过。

---

## 下一步待办

1. **前端交互打磨 (Polish)**: 恢复在 git checkout 中丢失的分栏拖拽重建 (split-pane drag)，提升加载状态 (Loading Skeleton) 等交互体验。
2. **文件夹库验证**: 完整跑通新建库/切换库/移除库等流程。
