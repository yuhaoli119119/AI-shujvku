# PDF 文献端到端验收报告（2026-06-01）

## 结论

PDF 文献入库、解析、人工校验队列、翻译预览、DFT 数据抽取和前端展示流程已经在当前工作树中完成修复，并用两篇新找的真实开放 PDF 完成隔离端到端验收。

本次验收没有写入 active 文献库，全部数据写入隔离目录：

- 验收脚本：`test-artifacts/pdf-regression/run_new_real_papers_e2e.py`
- 最新验收目录：`test-artifacts/pdf-regression/new_real_1780310429/`
- 隔离数据库：`test-artifacts/pdf-regression/new_real_1780310429/regression.sqlite`
- 隔离 storage：`test-artifacts/pdf-regression/new_real_1780310429/storage/`
- 机器结果：`test-artifacts/pdf-regression/new_real_1780310429/summary.json`

GROBID 在本地不可用时会记录 502 fallback 日志，但不会阻断流程；Docling 本地解析继续完成入库、正文、图表、Markdown 和 DFT 抽取。Writer LLM 未配置时，翻译预览返回 `local_fallback` 和 `source_only_writer_llm_not_configured`，前端会显示可读状态提示，不再 400 阻断。

## 新真实文献

1. `Rational Design of Non-Noble Metal Single-Atom Catalysts in Lithium-Sulfur Batteries through First Principles Calculations`
   - DOI：`10.3390/nano14080692`
   - 年份/期刊：2024, Nanomaterials
   - PDF：`test-artifacts/pdf-regression/new_real_papers/mdpi_nanomat_vs2_sac_lis_2024.pdf`
   - 来源：Semantic Scholar PDF 镜像；MDPI 直链在当前环境返回 Access Denied

2. `First-Principles Investigation of Phosphorus-Doped Graphitic Carbon Nitride as Anchoring Material for the Lithium-Sulfur Battery`
   - DOI：`10.3390/molecules29122746`
   - 年份/期刊：2024, Molecules
   - PDF：`test-artifacts/pdf-regression/new_real_papers/mdpi_molecules_p_gcn_lis_2024.pdf`
   - 来源：Semantic Scholar PDF 镜像；MDPI 直链在当前环境返回 Access Denied

PDF 文件有效性：

- `mdpi_nanomat_vs2_sac_lis_2024.pdf`：`%PDF-`，11 页，3,664,778 bytes
- `mdpi_molecules_p_gcn_lis_2024.pdf`：`%PDF-`，20 页，9,752,921 bytes

## 端到端验收结果

| 文献 | Sections | Tables | Figures | Figure images | DFT settings | DFT results | Review queue | Manual verified | Locators | Translation | Validation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| Nanomaterials VS2 SAC Li-S | 15 | 0 | 4 | 4 | 1 | 14 | 121 | 1 safe verified | 60 | fallback, 2 items | validated |
| Molecules P-g-C3N4 Li-S | 36 | 4 | 12 | 12 | 1 | 17 | 107 | 1 safe verified | 89 | fallback, 2 items | validated |

DFT 抽取覆盖：

- Nanomaterials 样本：`adsorption_energy=6`、`gibbs_free_energy_change=1`、`reaction_barrier=4`、`charge_density_difference_claim=3`
- Molecules 样本：`reaction_barrier=4`、`charge_transfer=2`、`dos_claim=4`、`charge_density_difference_claim=1`、`limiting_potential=6`

人工校验边界：

- `prepare_pending_reviews` 先生成 pending 队列。
- 两篇新文献随后各模拟一次受控人工确认，均成功生成 `verified_reviews=1`、`safe_verified_reviews=1`。
- 手动确认目标均为 `dft_settings.software`，并且通过 evidence reference 和 evidence text 检查。
- 未出现自动 verified、自动 safe_verified 或绕过人工确认的情况。

Schema/校验状态：

- 两篇新文献均为 `validation_status=validated`。
- 两篇新文献最终 `warning_counts={}`。

## 本轮关键修复

- Docling 解析：补齐 page block、表格 markdown、图像 page/caption fallback、缺失 PDF 抛 `FileNotFoundError`、坏 PDF warning fallback。
- 图表链路：真实 PDF 能抽取 figure/table 记录和 figure image，前端详情页可展示原始 JSON。
- DFT 结果：支持 Unicode 负号、避免 section/markdown 重复扫描、支持 ORR/电催化 `UL/eta/PDS` 表格、增加 `reaction_step` 字段。
- DFT 数值质量：修复 `0.5-7.1 eV` 范围横线误读为 `-7.1 eV`；统一 LiPS 词表，清掉 `unknown_adsorbate` 警告。
- DFT 设置：支持 `Ry -> eV`、`nm -> A` 换算，并保存 supporting text。
- Stage2：文献分类较弱时仍可基于强电化学信号触发电化学抽取。
- 翻译预览：Writer LLM 未配置时返回源文占位和明确状态，不再阻断前端。
- 前端：修复文献库加载函数暴露、分页/筛选、详情页翻译状态提示、工作台 schema value、DFT 质量面板中文状态、前端 smoke 测试选择器。

## 验证命令

后端：

- `python -m compileall app` 通过
- `python -m pytest ...` 分 6 批覆盖全部后端测试，当前合计 `504 passed`
- 受影响测试补充：`tests/test_dft_results_extractor.py tests/test_extraction_pipeline.py tests/test_p1_extractors.py tests/test_paper_translation.py ...`，`21 passed`

前端：

- `node --check tests/smoke.spec.js` 通过
- `npm run test -- --reporter=line`，`108 passed`

真实文献验收：

- `python test-artifacts/pdf-regression/run_new_real_papers_e2e.py` 通过
- 最新结果：`test-artifacts/pdf-regression/new_real_1780310429/summary.json`

## 已知限制

- 当前环境没有 GROBID 服务，元数据主要依赖外部 metadata、Docling 文本和 fallback；这不是阻断项，但日志会出现 GROBID 502。
- Writer LLM 未配置，因此本次证明的是翻译入口稳定和未配置 fallback 可用，不证明真实 LLM 翻译质量。
- DFT 抽取仍是规则优先，适合稳定抓取显式数值和证据；复杂表格、多值对应关系和同句多材料精确归属仍建议保留人工校验队列。
