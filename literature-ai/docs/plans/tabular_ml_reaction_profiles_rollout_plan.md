# Li-S SRR 优先的多反应表格型 DFT 机器学习实施计划

> 状态：讨论稿  
> 日期：2026-06-23  
> 当前主线：单/双原子催化剂用于 Li-S 电池硫氧化还原反应（SRR_LiS）  
> 扩展目标：后续支持 HER、OER、ORR、CO2RR 等反应，不复制多套解析管线

## 1. 结论先行

本轮不重写现有 PDF/DFT 解析器，不推倒数据库，不要求原子结构文件。实施方向是：

```text
一套公共 DFT 解析管线
+ 可配置的 ReactionProfile
+ 结果级 reaction_type
+ 反应专用验证器
+ 现有 safe_verified / evidence / locator 安全门
+ 面向表格型 ML 的任务级导出
```

架构一次支持 `SRR_LiS`、`HER`、`OER`、`ORR`、`CO2RR`、`UNKNOWN`。首个生产级 profile 只启用 `SRR_LiS`，其他 profile 先作为兼容框架和回归测试骨架，达到各自金标准验收后再标记为生产可用。

## 2. 已确认的现有基线

现有系统已具备：

- `DFTResult`、`DFTSetting`、`CatalystSample` 基础数据模型。
- DFT 数值、单位、吸附物、反应步骤和证据文本抽取。
- 材料身份、人工 `safe_verified`、证据文本和 PDF 精确页定位安全门。
- export v2 的 property/adsorbate canonicalization、单位归一化、setting 关联推断、`ml_blockers` 和 `is_ml_ready`。
- 结果级 descriptor 归属和歧义阻断。
- embedding v1 的 1024 维强制契约和回归测试。

当前主要缺口：

- 没有结果级 `reaction_type`，通用化学归一化会同时识别 Li-S、`*H`、`*OH/*OOH`、`*COOH` 等跨反应物种。
- DFT 抽取提示词与规则偏通用，没有将目标反应作为上下文。
- 一条 DFT 持久化失败仍可能中断整个 Stage 2。
- 每篇文献的处理结果只有实体数量，缺少反应分类、拒绝原因和表格 ML 就绪数量。
- export v2 判断了标签和证据是否可用，但没有任务级表格特征完整度判断。

## 3. 目标与非目标

### 3.1 本轮目标

1. 默认将 Li-S 文献中的 DFT 结果按 `SRR_LiS` 规则分类和验证。
2. 为 HER、OER、ORR、CO2RR 预留同一接口和配置格式。
3. 新老数据兼容，既有数值、单位、证据和人工 review 不被重写。
4. 一条 DFT 候选失败不影响整篇文献的其他数据入库。
5. 生成可直接被 pandas/Notebook 使用的 SRR_LiS 表格型 ML 数据集。
6. 数据集能区分“标签可用”和“表格特征足够”。
7. 数据集可复现，可追溯到 profile 版本、来源证据和导出参数。

### 3.2 本轮非目标

- 不做原子结构 GNN、通用机器学习势或 CIF/POSCAR 自动重建。
- 不把图像像素自动读数作为 ML 数值事实。
- 不把 AI 候选自动升级为 `safe_verified`。
- 不为每类反应复制一套 extractor/service/table。
- 不立即建立完整 `MaterialStructure`、`SurfaceModel` 或 `CalculationRun` 模型。
- 不在首个版本承诺 HER/OER/ORR/CO2RR 的文献级精度已达到生产标准。

## 4. 数据质量分层

后续不再用一个 `is_ml_ready` 回答所有问题，而是分为四层：

| 层级 | 回答的问题 | 核心条件 |
| --- | --- | --- |
| `candidate_valid` | 这是否像一条合法 DFT 候选 | property/value/unit/evidence 基本可解析 |
| `reaction_valid` | 这条数据是否属于指定反应 | reaction profile 、物种、property 和 step 一致 |
| `label_ready` | 这个目标值能否当训练事实 | 现有 safe_verified、evidence、exact locator、normalized target、linked setting |
| `tabular_ml_ready` | 这条记录是否有足够的表格特征 | 任务定义的必需特征不缺失，且无实例级歧义 |

`label_ready` 应复用现有 export v2 安全语义。`tabular_ml_ready` 是任务级派生结果，不建议作为一个永久、全局不变的数据库字段。

## 5. 反应模板架构

### 5.1 公共接口

新增 `app/domain/reaction_taxonomy.py`，提供：

```python
class ReactionProfile:
    key: str
    version: str
    allowed_intermediates: set[str]
    property_aliases: dict[str, str]
    allowed_properties: set[str]
    canonical_units: dict[str, str]
    step_graph: dict[str, set[str]]
    required_context_terms: set[str]
    exclusion_context_terms: set[str]
    tabular_tasks: dict[str, "TabularTaskProfile"]
```

公共函数：

```python
get_reaction_profile(reaction_type)
normalize_reaction_type(text)
classify_reaction_record(candidate, paper_context)
normalize_intermediate(reaction_type, text)
normalize_property_type(reaction_type, text)
validate_reaction_record(reaction_type, candidate)
```

### 5.2 支持级别

| ReactionProfile | 首发状态 | 首批覆盖 |
| --- | --- | --- |
| `SRR_LiS` | `production` | S8/Li2Sx、吸附能、反应自由能、Li2S 分解/成核能垒、迁移能垒、d-band/Bader/charge transfer |
| `HER` | `experimental` | `*H`、hydrogen adsorption/free energy、HER overpotential |
| `OER` | `experimental` | `*OH/*O/*OOH`、step free energy、limiting potential、overpotential |
| `ORR` | `experimental` | O2/`*OOH/*O/*OH`、step free energy、limiting/onset potential |
| `CO2RR` | `experimental` | `*COOH/*CO/*OCHO` 等中间体、吸附/自由能、limiting potential |
| `UNKNOWN` | `quarantine` | 保留候选，不进入反应专用 ML 导出 |

“代码中有 profile”不等于“该 profile 已经过文献金标准验收”。生产状态必须单独验收。

### 5.3 反应范围的判定原则

- 文献级可指定 `target_reaction=SRR_LiS`，但每条 DFTResult 仍必须独立标记 `reaction_type`。
- 一篇文献可包含多个反应。例如主文研究 CO2RR，同时报告 HER 竞争反应。
- 非目标反应候选不直接删除，而是标记 `out_of_scope` 或归入其他 profile。
- 只有在物种、property、reaction step 和语境一致时，才允许 `reaction_valid=true`。
- 跨反应共用物种（如 `*OH`）不能单独决定 reaction_type，必须结合上下文。

## 6. 数据库最小增量变更

### 6.1 首发必需字段

在 `dft_results` 增加 nullable 字段，不修改旧字段语义：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `reaction_type` | `VARCHAR(32)`, indexed, nullable | `SRR_LiS/HER/OER/ORR/CO2RR/UNKNOWN` |
| `reaction_type_source` | `VARCHAR(32)`, nullable | `extractor/rule/backfill/human` |
| `reaction_type_confidence` | `FLOAT`, nullable | 反应归属信心，不代表人工验证 |
| `reaction_profile_version` | `VARCHAR(64)`, nullable | 支持可复现导出和重新分类 |
| `reaction_validation_status` | `VARCHAR(32)`, nullable | `valid/out_of_scope/ambiguous/unsupported/error` |

不使用 PostgreSQL DB enum，使用应用层 Literal/validator，避免后续增加 NRR 等反应时频繁改 DB enum。

### 6.2 首发不重复存储的字段

以下数据继续由 export 或任务 profile 派生，避免与原始值漂移：

- `canonical_property_type`
- `canonical_adsorbate`
- `normalized_value/normalized_unit`
- `ml_blockers`
- `label_ready`
- `tabular_ml_ready`
- `ml_quality`

导出必须同时保留 raw 和 derived 值，并记录归一化/profile 版本。

### 6.3 可延后的关系强化

`DFTResult.dft_setting_id` nullable FK 对严格 ML 有长期价值，但不阻塞首个可用版本。首发仍可使用 export v2 的唯一 setting 推断与歧义阻断；在多 setting 文献比例较高时再升级为显式 FK。

## 7. 现有数据回填策略

既有数据是资产，回填必须“只补充、不改写”。

### 7.1 回填不变式

- 不修改 `value`、`unit`、`property_type`、`adsorbate`、`reaction_step` 原始值。
- 不修改现有 review、safe_verified、evidence locator 状态。
- 不因新 profile 删除旧 DFTResult。
- 只回填新的 reaction 字段和可审计报告。
- 回填重跑必须幂等，新 profile 版本不能静默覆盖人工标记。

### 7.2 回填步骤

1. 产生数据库备份或最少产生 DFT 表导出快照。
2. 运行 `audit_reaction_backfill --dry-run`，不写库。
3. 输出按 profile、confidence 和原因分组的统计。
4. 抽查 `SRR_LiS`、跨反应和 `ambiguous` 候选。
5. 只对确定性规则匹配记录自动回填，例如有明确 Li2Sx + adsorption/barrier 语境。
6. 语境不足的记录标记 `UNKNOWN/ambiguous`，不猜测。
7. 应用回填后重新生成质量报告，确认原始数据行数和 review 数未变。

### 7.3 dry-run 报告最小字段

```json
{
  "profile_version": "reaction_profiles_v1",
  "total_records": 0,
  "unchanged_human_labels": 0,
  "classifiable": 0,
  "ambiguous": 0,
  "unsupported": 0,
  "by_reaction_type": {},
  "by_reason": {},
  "sample_record_ids": {}
}
```

## 8. 新文献解析流程

```text
PDF/TEI/Docling
  -> 公共 DFT raw candidate extractor
  -> reaction classifier
  -> ReactionProfile normalization/validation
  -> 逐条 savepoint 持久化
  -> 证据定位与人工 review
  -> export safety gate
  -> TabularTaskProfile feature gate
  -> CSV/JSON/manifest
```

关键规则：

- extractor 负责“尽量找到原始候选”，profile validator 负责“是否属于当前反应”。
- 过早硬过滤会丢数据，因此不属于 SRR 的合法候选应保留为其他 profile 或 `out_of_scope`。
- 一条持久化失败应通过 `session.begin_nested()` 隔离，并继续处理其他候选。
- 反应 validator 不设置 review 为 verified，只生成候选级 validation 状态。
- 图注中明确的文本数值可继续作为候选证据，但 image-only 不作为 ML 标签。

## 9. 表格型 ML 数据契约

### 9.1 首发任务

第一个可用任务建议限定为：

```text
reaction_type = SRR_LiS
catalyst_scope = SAC | DAC
target = adsorption_energy | reaction_barrier
model_family = CatBoost | XGBoost | RandomForest baseline
```

在数据量足够前，吸附能和能垒不混成一个目标模型。不同 adsorbate/intermediate 可做多任务特征，也可按数据量分成单独任务。

### 9.2 通用特征层

| 类型 | 字段 | 首发要求 |
| --- | --- | --- |
| 身份 | catalyst identity / sample id | 必需 |
| 反应 | reaction_type、canonical adsorbate/intermediate、reaction step | 必需 |
| 目标 | canonical property、normalized value/unit | 必需 |
| 催化剂 | SAC/DAC、metal centers | 必需 |
| 局部环境 | coordination、support | 推荐，缺失时产生 feature blocker |
| 计算设置 | functional、dispersion、pseudopotential、cutoff、k-points | linked setting 必需，内部字段允许缺失并显式 mask |
| 可选描述符 | d-band center、Bader charge、charge transfer | 只从同一 instance scope 关联 |
| 溯源 | paper/evidence/page/review/profile version | 必需，不直接作为数值特征 |

### 9.3 特征就绪判定

每个 `TabularTaskProfile` 定义自己的：

```text
required_features
optional_features
allowed_targets
allowed_units
feature_blockers
split_group_keys
```

建议输出：

```text
label_ready
tabular_ml_ready
label_blockers[]
feature_blockers[]
task_profile
task_profile_version
```

例如 `missing_coordination` 不必然意味着标签错误，但可能使某个配位环境模型无法使用该条数据。

### 9.4 数据划分安全

- 不按 DFTResult 随机拆分训练/测试集。
- 最少按 `paper_id` 分组，防止同一论文重复结果泄漏。
- 主结论建议再按 catalyst/material family 分组验证。
- 同一个催化剂不同 adsorbate 如果分到训练和测试两侧，必须明确这是任务设计，不得默认为独立样本。

## 10. ML 导出版本

保留 `dft_results_ml_v2` 不变，新增 `dft_results_ml_v3`，避免旧 Notebook 静默改变语义。

v3 在 v2 基础上增加：

- reaction profile 元数据。
- result-level reaction classification 与 validation status。
- task profile 与 feature blockers。
- `label_ready` 与 `tabular_ml_ready` 分层。
- split group keys。
- dataset manifest，包含 schema/profile/normalization 版本、filters、时间、记录数和代码版本。

首发提供：

```text
GET /api/dft/ml-dataset-v3?reaction_type=SRR_LiS&task=adsorption_energy
GET /api/dft/ml-dataset-v3?reaction_type=SRR_LiS&task=reaction_barrier
```

导出格式：

- JSON：保留完整层级、blockers 和 provenance。
- CSV：打平为单任务训练表。
- manifest JSON：记录数据集版本和筛选条件。

## 11. 每篇文献处理报告

首发不新建报告表，优先复用现有 job progress、AuditLog 或结构化响应。最小报告：

```json
{
  "paper_id": "...",
  "target_reaction": "SRR_LiS",
  "profile_version": "reaction_profiles_v1",
  "parse_status": "success|partial_success|failed",
  "dft_candidates_total": 0,
  "reaction_valid": 0,
  "out_of_scope": 0,
  "ambiguous": 0,
  "persisted": 0,
  "persistence_errors": 0,
  "label_ready": 0,
  "tabular_ml_ready_by_task": {},
  "rejection_reasons": {}
}
```

## 12. 实施阶段与粗略工期

以一名熟悉当前代码的开发者估算：

| 阶段 | 工作 | 预估 |
| --- | --- | --- |
| 0. 契约冻结 | 确认字段、profile 接口、v3 格式、备份策略 | 0.5-1 天 |
| 1. 反应域层 | ReactionProfile 注册表、五类 profile、validator、单元测试 | 2-3 天 |
| 2. 增量 schema | nullable 字段、迁移、API schema、兼容测试 | 1-2 天 |
| 3. 解析管线 | target reaction 传递、逐条容错、处理报告 | 2-3 天 |
| 4. ML export v3 | task profiles、feature blockers、JSON/CSV/manifest、回归测试 | 2-3 天 |
| 5. 旧数据回填 | dry-run 工具、抽查、应用回填、质量对比 | 2-4 天 |
| 6. 使用入口 | 前端筛选/导出入口、最小 Notebook baseline | 1-2 天 |

预期：

- 只求 SRR_LiS API/CSV 可用：约 7-10 个工作日。
- 包含回填、前端入口和其他 profile 骨架：约 10-15 个工作日。
- 其他反应 profile 达到生产精度：需要额外的真实文献金标准集，按 profile 单独估算。

最快投入使用的方式是先交付后端 API + CSV + 质量报告，不让完整前端面板阻塞第一次建模。

## 13. 预计修改范围

### 后端

- `app/domain/reaction_taxonomy.py`
- `app/services/reaction_record_validator.py`
- `app/extractors/dft_results_extractor.py`
- `app/services/extraction_pipeline.py`
- `app/db/models.py`
- `app/db/session.py`
- `app/migrations/*.sql`
- `app/services/dft_export_service.py`
- `app/schemas/dft_export.py`
- DFT export/API router

### 工具

- `tools/audit_reaction_backfill.py`
- `tools/backfill_dft_reaction_type.py`
- 最小 Notebook 或 Python baseline 脚本

### 测试

- `tests/test_reaction_taxonomy.py`
- `tests/test_reaction_record_validator.py`
- `tests/test_dft_reaction_backfill.py`
- `tests/test_dft_extraction_fault_tolerance.py`
- `tests/test_export_safety_gate.py`
- `tests/test_dft_ml_dataset_v3.py`

## 14. 验收标准

### 14.1 兼容与安全

- 旧 DFTResult 无需 reaction 字段也能正常读取。
- 迁移前后原始 DFT 行数、value/unit 和人工 review 不变。
- dry-run 默认不写库，应用回填需要显式参数。
- 任何新规则不会自动产生 safe_verified。
- export v2 语义和测试不回退。

### 14.2 反应分类

- SRR_LiS 任务默认不导出 `*H`、`*OH`、`*OOH`、`*COOH` 等明确属于其他反应的记录。
- Li2S6/Li2S4 吸附能能通过 SRR_LiS profile。
- Li2S 分解/成核能垒能映射到正确 property subtype。
- 混合反应文献可在同一 paper 下保留不同 reaction_type 的记录。
- 无法确定的记录进入 `UNKNOWN/ambiguous`，不猜测。

### 14.3 容错

- 一篇文献中一条 DFTResult 持久化失败时，其他候选仍可入库。
- 处理结果标记 `partial_success`，并返回可定位的错误原因。
- 重试不产生重复 DFT 候选。

### 14.4 ML 导出

- SRR_LiS CSV 不包含其他 reaction_type。
- 默认训练 CSV 只包含 `label_ready=true` 且 `tabular_ml_ready=true` 的数据。
- raw value/unit 与 normalized value/unit 同时保留。
- 每行可追溯至 paper、evidence、PDF page、review 和 profile version。
- 按 paper/material family 生成的 split key 可直接被 baseline 脚本使用。
- 使用只包含 SRR_LiS 的最小 fixture 可完成 pandas 加载和一次 baseline 训练。

## 15. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 过度硬过滤丢失可用候选 | 保留 raw candidate，使用 out_of_scope/ambiguous，不直接删除 |
| 一篇文献含多反应 | 使用 result-level reaction_type，paper target 只作为上下文 |
| 相同中间体跨反应出现 | 将物种、property、step 和上下文联合判定 |
| 旧数据被新规则误改 | dry-run、只回填新字段、保护人工来源、记录 profile version |
| 导出记录有标签但没有特征 | 拆分 label blockers 与 feature blockers |
| 小数据集测试分数虚高 | 按 paper/material family 分组拆分，保留外部验证集 |
| 其他 reaction profile 表面可用但未验证 | 使用 production/experimental 状态，按 profile 单独验收 |

## 16. 需要讨论确认的决策

### 决策 1：默认反应范围放在哪里

建议：文献库或解析任务可设置 `target_reaction=SRR_LiS`，每条 DFTResult 仍独立分类。不建议只在 Paper 上放唯一 reaction_type。

### 决策 2：旧数据自动回填边界

建议：只对确定性匹配自动写入；语境性匹配输出为建议；歧义项保持 UNKNOWN。

### 决策 3：是否立即增加 `dft_setting_id`

建议：不阻塞首发。先运行 dry-run 统计多 setting 歧义比例，若成为主要 blocker，再在 v3 上线前增加 nullable FK。

### 决策 4：v2 扩展还是 v3

建议：新建 v3。反应语境和任务级特征就绪会改变消费者语义，不应在 v2 中静默改变。

### 决策 5：首个 ML 目标

建议：优先选择数据量最多、定义最稳定的一个目标。候选顺序：

1. Li2Sx adsorption energy。
2. Li2S decomposition/nucleation barrier。
3. 其他 reaction free energy/barrier。

最终顺序应由 dry-run 质量统计决定，不应在不看现有数据分布的情况下预先锁死。

## 17. 建议的第一个 Sprint

为最快投入使用，第一个 Sprint 只做以下闭环：

1. 冻结 ReactionProfile 和 DB 增量字段。
2. 实现五类 profile 骨架与 SRR_LiS 完整 validator。
3. 实现逐条 DFT 持久化容错。
4. 实现旧数据 reaction backfill dry-run，不写库。
5. 用 dry-run 统计选择第一个 ML target。
6. 实现该 target 的 v3 JSON/CSV 导出。
7. 提供一个最小 pandas + CatBoost/XGBoost baseline 脚本验证导出可用性。

该 Sprint 完成后，可以用现有数据开始第一次表格型 ML 实验，同时不锁死后续反应类型。

## 18. 实施进度跟踪

### 18.1 已确认决策

2026-06-23 确认：

- 采用“后端 API/CSV 优先”，不让完整前端阻塞首次 ML 使用。
- 旧数据回填默认 dry-run。
- 只自动回填确定性反应分类，歧义记录保持 `UNKNOWN/ambiguous`。
- 公共框架一次支持多反应，首个生产级 profile 为 `SRR_LiS`。
- 当前系统仍可继续作为通用 DFT 解析与人工复核工作台使用；本计划是增强表格型 ML 专用能力，不是修复“当前系统无法使用”。

### 18.2 阶段状态

| 阶段 | 状态 | 当前说明 |
| --- | --- | --- |
| 0. 契约冻结 | `verified` | ReactionProfile v1 公共接口已通过总控验收 |
| 1. 反应域层 | `verified` | 五类 profile 骨架、SRR_LiS validator 及边界修复已通过 54 项测试 |
| 2. 增量 schema | `verified` | 五个 nullable reaction 字段、baseline/runtime migration、幂等索引和真实库最小迁移均已通过验收 |
| 3. 解析管线 | `verified` | target reaction 传递、逐条容错和处理报告均已通过总控验收 |
| 4. ML export v3 | `verified` | v3 JSON service、严格 contract、受保护 JSON API、CSV 与 manifest 下载入口均已通过总控验收 |
| 5. 旧数据回填 | `verified` | 确定性 valid reaction 回填已执行并验收；S8/Figure S8 误伤已修复并清理，当前保留 6 条 HER/ORR rule/valid，SRR_LiS 可训练行仍为 0 |
| 6. 使用入口 | `verified` | v3 CSV 最小 pandas/numpy baseline 与前端 v3 SRR_LiS 使用入口均已 verified；当前真实库无 SRR_LiS 训练行时，页面明确显示为空数据/待复核状态而非导出失败 |

### 18.3 实施与验收记录

| 日期 | 工作项 | 负责 | 状态 | 证据/备注 |
| --- | --- | --- | --- | --- |
| 2026-06-23 | 编写多反应表格型 ML 实施计划 | 总控线程 | `completed` | 本文档 |
| 2026-06-23 | ReactionProfile 域层、五类 profile 骨架、SRR_LiS validator 与单元测试 | 实施线程 `019ef0e8-a1ac-72a0-a856-856a5c9bd434` | `verified` | 总控复测 54 passed；compileall、diff check 和三个原始失败用例全部通过 |
| 2026-06-23 | 创建实施线程监控、验收与续任 heartbeat | 总控线程 | `stopped` | automation `reactionprofile` 已按用户要求删除，后续改为手动总控 |
| 2026-06-23 | 增加实施线程上下文轮换策略 | 总控线程 | `active` | 同一线程最多承担两个已验收实施批次 |
| 2026-06-23 | 阶段 1 首轮总控验收 | 总控线程 | `verified_after_repair` | HER 子串误判、descriptor intermediate 规则、binding energy 语义已修复并复测通过 |
| 2026-06-23 | 增量 reaction schema 字段与迁移契约 | 实施线程 `019ef0e8-a1ac-72a0-a856-856a5c9bd434` | `verified` | 总控复测 67 passed；隔离 schema 连续两次迁移、compileall 和 diff check 通过；当前 DB 未迁移 |
| 2026-06-23 | 实施线程轮换 | 总控线程 | `completed` | 旧线程 `019ef0e8-a1ac-72a0-a856-856a5c9bd434` 已完成两个验收批次；新线程 `019ef108-8e8d-7a00-bb3c-c659a9808b6d` |
| 2026-06-23 | 新候选 reaction 持久化与逐条 savepoint 隔离 | 实施线程 `019ef108-8e8d-7a00-bb3c-c659a9808b6d` | `verified` | 总控复测 59 passed；compileall、diff check 通过；修复旧 reaction_type 与 incoming 元数据混搭风险；未修改 DB schema、export 或当前数据 |
| 2026-06-23 | 调整推理等级策略 | 总控线程 | `active` | 总控验收/诊断可使用 high 或 xhigh；发送到实施窗口的状态检查使用 low，实施、修复和新窗口统一使用 medium |
| 2026-06-23 | 调整 heartbeat 间隔 | 总控线程 | `stopped` | automation `reactionprofile` 曾由每 5 分钟改为每 10 分钟，现已删除 |
| 2026-06-23 | 每篇处理报告与 partial_success 错误可见性 | 实施线程 `019ef108-8e8d-7a00-bb3c-c659a9808b6d` | `verified` | 总控复测 63 passed；compileall、diff check 通过；保留旧计数字段，新增 dft_processing_report 和可定位 persistence error；未修改 DB schema、export、安全门或当前数据 |
| 2026-06-23 | 实施线程轮换 | 总控线程 | `completed` | 旧线程 `019ef108-8e8d-7a00-bb3c-c659a9808b6d` 已完成两个验收批次；新线程 `019ef264-12e8-7ac0-80d1-5e775ceb08c2`；轮换原因为达到两批上限 |
| 2026-06-23 | 文献级 target_reaction 上下文传递 | 实施线程 `019ef264-12e8-7ac0-80d1-5e775ceb08c2` | `verified` | 总控复测 67 passed；compileall、diff check 通过；候选明确分类优先，歧义候选使用 paper target 消歧；无效 target 隔离为 UNKNOWN；未修改实际数据库、export、安全门或当前数据 |
| 2026-06-23 | 旧数据 reaction backfill dry-run 工具 | worker `019ef26e-0830-7663-ad77-ba44c7e61e0a` | `verified` | 总控复测 57 passed；compileall、diff check 通过；inspector + 白名单 SELECT 兼容未迁移旧 schema；隔离测试证明无 DML/DDL 且前后数据不变；未扫描当前实际数据库 |
| 2026-06-23 | 通用 TabularTaskProfile 契约与特征门 | worker `019ef26e-0830-7663-ad77-ba44c7e61e0a` | `verified` | 总控复测 60 passed；compileall、diff check 通过；两个 task 均保持 candidate，label/feature blockers 分层且输出稳定；未修改 export v2、DB、API 或当前数据 |
| 2026-06-23 | worker 轮换 | 总控线程 | `completed` | worker `019ef26e-0830-7663-ad77-ba44c7e61e0a` 已完成两个 verified 批次并关闭；新 worker `019ef28e-b64d-7880-842a-b597cf12b6f4`，轮换原因为达到两批上限 |
| 2026-06-23 | ML dataset v3 JSON service 与 manifest | worker `019ef28e-b64d-7880-842a-b597cf12b6f4` | `verified_after_repair` | 总控发现并退修筛选前 limit 截断及单数 dft_result 页码遗漏；修复后总控复测 67 passed，compileall、diff check 通过；v2 非破坏性，未修改 API、CSV、DB 或当前数据 |
| 2026-06-23 | ML dataset v3 Pydantic contract 与 JSON API | worker `019ef28e-b64d-7880-842a-b597cf12b6f4` | `verified` | 总控复测 68 passed；compileall、diff check 通过；新增严格 v3 Pydantic contract、`select_training_records_v3`、受 export policy 保护的 `/api/dft/ml-dataset-v3`；task 必填、未知 task/越界 limit 返回 422；v2 回归通过；未做 CSV、前端、DB 或真实数据审计 |
| 2026-06-23 | ML dataset v3 CSV 与 manifest 下载入口 | worker `019ef2b2-4649-72b2-8be0-5b4d8240f724` | `verified` | 总控复测 22 passed；Stage 4 宽回归 71 passed；compileall、diff check 通过；新增 `build_dft_ml_dataset_v3_csv`、`/api/dft/ml-dataset-v3.csv` 与 `/api/dft/ml-dataset-v3/manifest`，CSV 默认只导出 label_ready 且 tabular_ml_ready 记录；未修改真实数据库、前端、MCP、Notebook 或回填 |
| 2026-06-23 | v3 CSV 最小 pandas/numpy baseline | worker `019ef2b2-4649-72b2-8be0-5b4d8240f724` | `verified` | 总控复测 4 passed；导出相关宽回归 75 passed；compileall、diff check 通过；新增无 sklearn 依赖的 `tools/ml_baseline_srr_lis.py`，按 `split_paper_id` 分组拆分并过滤非 ready 行；未修改 API、DB、回填、前端或 MCP |
| 2026-06-23 | 当前真实库 reaction dry-run 与 v3 ML 可用性审计 | worker `019ef2be-ac6c-7691-9a1f-f0b3b8da1c62` | `verified` | 总控复跑 dry-run 通过；报告写入 `docs/audits/dft_reaction_ml_readiness_2026-06-23.md` 和 `docs/audits/dft_reaction_backfill_dryrun_2026-06-23.json`；当前库 668 条 DFT，SRR_LiS dry-run 5 条，validator-valid 仅 1 条 adsorption_energy；真实库缺 5 个 reaction 字段导致 v3 live export 暂不可用；未执行 DB 写入、迁移或回填 |
| 2026-06-23 | 当前真实库 reaction nullable schema 迁移 | worker `019ef2be-ac6c-7691-9a1f-f0b3b8da1c62` | `verified` | 总控复查真实库 row count 668 不变；5 个 reaction 字段全部 present；`ix_dft_results_reaction_type` 存在；dry-run `writes_performed=0`；v3 live manifest/CSV 可运行但因未回填均被 `unknown_reaction_type` 排除；报告写入 `docs/audits/reaction_schema_live_migration_2026-06-23.md`；未执行 UPDATE/INSERT/DELETE、回填或代码修改 |
| 2026-06-23 | 安全 reaction backfill apply 工具 | worker `019ef2c9-03a9-7f20-b23c-06ba37666c33` | `verified` | 总控复测 20 passed；compileall、diff check 通过；新增默认 dry-run、显式 `--apply` 才写的 `tools/backfill_dft_reaction_type.py`；只写 5 个 reaction 字段，保护 human/manual，默认跳过已有 reaction_type；尚未执行真实库 apply |
| 2026-06-23 | 真实库 reaction backfill apply 首次尝试 | 总控线程 | `blocked_repairing` | `--apply` 因 PostgreSQL UUID id 与 VARCHAR 参数比较失败而回滚；总控复查 `reaction_type_nonnull=0`、`rule_sources=0`、总行数 668，确认未写入；已退修 worker `019ef2c9-03a9-7f20-b23c-06ba37666c33` 修复 UUID 类型比较 |
| 2026-06-23 | 修复 backfill apply PostgreSQL UUID 更新 | worker `019ef2c9-03a9-7f20-b23c-06ba37666c33` | `verified` | 总控复测 21 passed；compileall、diff check 通过；工具改为反射真实表并按实际 rowcount 报告，避免 UUID 与 VARCHAR 比较错误；未执行真实库 apply |
| 2026-06-23 | 真实库确定性 reaction backfill apply | 总控线程 | `verified` | 先保存 dry-run 报告，再显式执行 `--apply`；写入 7 条 rule/valid reaction 字段（HER 2、ORR 4、SRR_LiS 1），真实库总行数保持 668；二次 dry-run 显示 `eligible_updates=0`、`skipped_existing=7`；报告写入 `docs/audits/reaction_backfill_apply_2026-06-23.json`；未修改 value/unit/property/evidence/review/safe_verified |
| 2026-06-23 | S8/Figure S8 SRR_LiS 误伤修复与清理 | 总控线程 | `verified` | 发现唯一 SRR_LiS rule/valid 记录实际来自 CO2RR 论文的 `Figure S8` 文本；收紧 taxonomy，plain `S8` 无 Li-S 上下文不再作为 SRR 强信号；清理该记录 5 个 rule reaction 字段，原始 DFT 值和证据不变；复测 47 passed；清理后真实库保留 HER 2、ORR 4，SRR_LiS 0；报告写入 `docs/audits/reaction_backfill_s8_false_positive_cleanup_2026-06-23.md` |
| 2026-06-23 | 主线宽回归与当前可用性结论 | 总控线程 | `verified` | 主线宽回归 108 passed；compileall、diff check 通过；后端 v3 JSON/CSV/manifest、确定性 reaction 回填和最小 baseline 均可运行；live v3 SRR_LiS CSV 当前 0 行，原因是当前真实库没有安全可用的 SRR_LiS rule/valid 记录 |
| 2026-06-23 | 跳过数据补充后的前端 v3 使用入口 | worker `019ef2e2-a7ad-7550-a879-bd05fbfd6011` | `verified` | 总控复看 diff 边界并退修一次 v3 直接刷新/下载未读取当前 DOM 筛选的问题；复测 `npx playwright test tests/dft_ml_dataset.spec.js` 为 8 passed，`git diff --check` 通过；修改范围限于 `frontend/pages/dft_ml_dataset/index.html`、`page.js`、`page.css` 与 `frontend/tests/dft_ml_dataset.spec.js`；未修改后端、数据库、回填、export v2 或安全门 |

状态约定：`pending` -> `in_progress` -> `implemented` -> `verified`。代码完成不等于验收完成；只有总控线程完成 diff review、定向测试和回归测试后，才将阶段标记为 `verified`。

### 18.4 总控约定

- heartbeat `reactionprofile` 已按用户要求删除，不再定时检查。
- 当前目标 worker：无；worker `019ef2e2-a7ad-7550-a879-bd05fbfd6011` 已完成一个 verified 批次并关闭。
- 当前状态：Stage 0-6 均已 verified；后端 v3 JSON/CSV/manifest、确定性 reaction 回填、最小 baseline 与前端 v3 SRR_LiS 使用入口均可运行。当前 live v3 CSV 仍为 0 行，原因是当前真实库没有安全可用的 SRR_LiS rule/valid 记录；页面已将其显示为数据不足/待复核，而不是导出失败。
- 上一 API/schema 批次已由总控窗口手动读取、复测并验收完成。
- 从下一批开始优先使用总控窗口管理的 worker 子代理：完成结果自动回报总控，不需要用户转述或定时轮询。
- 总控验收通过后直接向同一 worker 下发下一批；验收失败时直接退回具体修复项。
- 计划书、阶段状态、验收结论和任务边界始终由总控窗口维护，worker 不得自行标记 verified。
- 实施线程未完成时不重复下发命令。
- 完成后由总控线程验收 diff、边界、定向测试和回归测试。
- 验收失败时只下发修复命令，不推进计划状态。
- 验收通过后下发下一批最小实施任务，并同步本文档。
- 总控线程自身做代码审查、失败诊断和验收判断可使用高或超高推理。
- 发送到实施线程的简单状态检查使用低推理；代码实施、修复、续任和新线程统一使用中推理；禁止给实施线程使用高、超高或 max。

### 18.5 实施线程轮换策略

- 同一实施线程或 worker 最多承担两个已验收的实施批次。
- 下发第三个实施批次前，必须新建中推理线程。
- 出现输出截断、历史过长、反复回读、任务边界混淆或重复错误时，不等到两批上限，立即轮换。
- 新线程不依赖旧对话，必须通过本计划书和自包含任务说明完成交接。
- 轮换时记录旧/新线程 ID、原因、已完成批次和新任务边界。
- worker 完成会自动回报总控；总控验收后使用直接消息续任或退修。
- 已换出的线程或已关闭的 worker 不再接收实施任务。

## 19. 锂硫双原子数据库建设计划

### 19.1 当前定位

2026-06-24 确认：

- “锂硫双原子”是当前阶段新建的独立项目库/文献库，库内文献主题集中在锂硫电池单/双原子催化剂。
- 当前阶段主任务是围绕该库建设 Li-S 电池单/双原子催化剂数据，优先服务 SRR_LiS 表格型机器学习。
- 该定位不代表整个系统只能做锂硫双原子催化剂；后续其他反应、其他专题库和通用功能仍需继续实现。
- 本库不是“全局总数据库”的替代品，也不要求全局数据库改成 Li-S 专用；实施时应把项目库上下文、解析提示词、审核提示词、质量统计和导出入口按“锂硫双原子”当前任务组织。

### 19.2 当前已完成基础

- 多反应 ReactionProfile 框架、SRR_LiS profile、结果级 reaction 字段、解析管线容错、v3 JSON/CSV/manifest 导出、最小 baseline 和前端 v3 入口均已 verified。
- 0090 文献已作为 SRR_LiS/DFT 审核链路压力测试使用，暴露并修复了拒绝状态、AI 审核计数、前端状态显示和导出口径问题。
- 当前系统已经具备承接“锂硫双原子”项目库建设的基础能力；后续重点应转为项目库数据建设、专题字段补齐、专题提示词和质量面板。

### 19.3 仍需实施的专题功能

| 批次 | 工作项 | 状态 | 说明 |
| --- | --- | --- | --- |
| L1 | 项目库上下文配置 | `pending` | 让“锂硫双原子”库默认携带 Li-S/SAC-DAC/SRR_LiS 上下文，用于解析、审核、筛选和导出 |
| L2 | 专题字段与数据字典 | `pending` | 定义金属中心、SAC/DAC、同核/异核、载体、配位环境、LiPS、Li2S、DFT 标签和实验性能字段 |
| L3 | 专题文献筛选与队列 | `pending` | 在该库内区分已导入、已解析、含 DFT、待审核、可导出、可训练、需补字段等状态 |
| L4 | SRR_LiS 专题解析/审核提示词 | `pending` | 优化提示词，重点检查 Li2Sx、Li2S 成核/分解、吸附能、迁移/反应能垒、配位结构和证据锚点 |
| L5 | SAC/DAC 结构特征抽取 | `pending` | 抽取 metal centers、coordination、support、M-M distance、single/dual atom type 等表格型 ML 特征 |
| L6 | 实验性能数据抽取 | `pending` | 抽取容量、倍率、循环稳定性、衰减率、硫负载、电解液/硫比等 Li-S 电池性能字段 |
| L7 | 项目库质量面板 | `pending` | 显示该库文献数、DFT 数、SRR_LiS 标签数、可训练行数、主要 blocker 和待补审文献 |
| L8 | 项目库 ML 导出与 baseline | `pending` | 输出面向“锂硫双原子”库的 SRR_LiS adsorption/barrier CSV，并运行最小 baseline |

### 19.4 下一窗口交接要求

- 新窗口首先必须 UTF-8 明读本计划书，并以本节作为最新任务边界。
- 新窗口不得把“锂硫双原子”误解为全局数据库改造；它是当前阶段的项目库建设任务。
- 新窗口第一批只允许做 L1/L2 的方案或最小实现，不得直接批量改写真实数据，不得自动标记 safe_verified/verified。
- 每个实现批次必须报告修改文件、测试结果、是否触碰真实数据库、是否影响 export v2、安全门和旧数据。
- 总控窗口负责验收并维护本计划表；实施窗口不得自行把状态改为 `verified`。
