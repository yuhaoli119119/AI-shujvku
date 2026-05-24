# AI 自动查文献 × 分级知识库 — 统一升级规划

> **定位**：将"AI 自动查文献"从「检索即落库」升级为「分类感知 → 差异化抽取 → 智能写作」的全链路智能化系统。
> 本规划只做设计，不改动代码。所有改动项标注优先级和工作量，供决策后按序实施。
> **当前版本**：v1.3a | **更新日期**：2026-05-23

---

## 一、现状诊断：功能已有哪些、缺什么

### 1.1 已实现功能清单

| 层 | 功能 | 实现位置 | 状态 |
|---|---|---|---|
| **检索层** | 自然语言 → LLM 改写 → 多源并发搜索 | `papers.py::_rewrite_ai_search_query()` + `DiscoveryService` | ✅ |
| **检索层** | 6 大搜索源（OpenAlex / arXiv / PubMed / Semantic Scholar / Crossref / X-MOL） | `findpapers/connectors/` + `discovery_service.py` | ✅ |
| **检索层** | 去重（DOI → title+year → identifier 三级） | `discovery_service.py::_dedupe_key()` | ✅ |
| **检索层** | 后台任务（非阻塞 + 轮询） | `papers.py::AI_WORKFLOW_JOBS` | ✅ |
| **入库层** | 下载 PDF → GROBID + Docling 双解析 → 结构化落库 | `paper_ingestion.py::ingest_pdf()` | ✅ |
| **入库层** | 元数据补全（DOI → 自动查询） | `paper_ingestion.py` | ✅ |
| **入库层** | 每篇文献永久序号（serial_number） | `paper_ingestion.py` | ✅ |
| **抽取层** | 7 大抽取器全量运行 | `extraction_pipeline.py::run_stage2()` | ✅ |
| **抽取层** | 11 类细粒度分类（A1-A4 / B1-B3 / C1-C3 / R） | `comprehensive_extractor.py` | ✅ |
| **抽取层** | type_confidence 置信度打分 | `comprehensive_extractor.py` | ✅ |
| **抽取层** | 按分类差异化字段（computational_details / experimental_details） | `comprehensive_analysis.py` schema | ✅ |
| **RAG 层** | 混合检索（词法 + embedding） | `retriever.py` | ✅ |
| **RAG 层** | Evidence pack 全局去重 + round-robin | `retriever.py::_global_dedup()` + `prompt_builder.py::_round_robin_by_paper()` | ✅ |
| **RAG 层** | Citation guard（数值级 + 事实级） | `citation_guard.py` | ✅ |
| **RAG 层** | Fact-claim repair（missing_fact_claims 修复） | `writer.py::_repair_section_with_rule_seed()` | ✅ |
| **前端** | 统一单页工作台（5 标签 + 库管理工具栏） | `literature_library/index.html` | ✅ |
| **前端** | AI 搜索 + AI 搜索并收录 | `index.html::runAISearch()` / `runAIWorkflow()` | ✅ |
| **桌面端** | AI自动查文献按钮 + QThread 后台执行 | `search_page.py::start_ai_workflow()` | ✅ |
| **桌面端** | 同步 LitAI 结果 / 发往 LitAI | `library_page.py` | ✅ |
| **桌面端** | Markdown 报告渲染（综合分析展示） | `extraction_page.py::JsonViewerDialog` | ✅ |

### 1.2 核心缺口

| # | 缺口 | 影响 | 涉及文件 |
|---|---|---|---|
| G1 | **抽取管线不感知分类** — 7 个抽取器全量运行，A 类跑电化学、C 类跑 DFT | 浪费 LLM 调用 + 引入噪声 | `extraction_pipeline.py` |
| G2 | **RAG 检索不感知分类** — 写电催化论文时混入纯 MD 模拟证据 | 写作质量下降 | `retriever.py` |
| G3 | **写作模板不感知分类** — A/C/R 类论文用同一套章节模板 | 生成内容偏离论文范式 | `writer.py` + `prompt_builder.py` |
| G4 | **前端无分类标签/筛选** — 综合分析只是裸 JSON 展示 | 用户无法按类型浏览文献 | `index.html` |
| G5 | **两套分类体系无映射** — WritingCard 4 类 vs Comprehensive 11 类 | 数据不一致 | `writing_card_extractor.py` |
| G6 | **AI 改写查询不感知分类** — 搜"电池"时不知道用户要 A 类还是 C 类 | 搜索精准度受限 | `papers.py::_rewrite_ai_search_query()` |
| G7 | **分栏拖拽丢失** — git checkout 事故 | 左侧栏宽度固定 | `index.html::initSplitDrag` |

---

## 二、核心设计：分类感知全链路

### 2.1 设计理念

当前链路：`查询 → 改写 → 搜索 → 入库 → 全量抽取 → 全量检索 → 固定模板写作`

目标链路：`查询(+意图分类) → 改写(感知分类) → 搜索(感知分类) → 入库 → 快速分类 → 差异化抽取 → 分类感知检索 → 分类适配写作`

**关键转变**：引入"分类"作为贯穿全链路的一等公民，而非事后标注。

### 2.2 全链路架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        用户输入                                      │
│  "帮我找 2024 年 CO2 电还原的 DFT 计算论文"                           │
│         ↕ 可选：用户指定目标分类 (A1/B1/C3/R...)                      │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                  ┌─────────▼─────────┐
                  │  Stage 0: 意图识别  │  ← 新增
                  │  LLM 快速判断      │
                  │  target_type +     │
                  │  search_strategy    │
                  └─────────┬──────────┘
                            │
              ┌─────────────▼──────────────┐
              │  Stage 1: 分类感知查询改写    │  ← 升级
              │  原：通用学术查询改写         │
              │  新：注入目标分类的领域词表    │
              │  A 类侧重 methodology 关键词 │
              │  C 类侧重 synthesis 关键词  │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  Stage 2: 分类感知搜索       │  ← 升级
              │  A 类：优先 arXiv           │
              │  B/C 类：优先 OpenAlex       │
              │  R 类：优先 OpenAlex (type:  │
              │        review)              │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  Stage 3: 入库 + 快速分类   │  ← 升级
              │  PDF 下载 → GROBID/Docling  │
              │  → ComprehensiveExtractor  │
              │    仅跑分类字段（轻量）       │
              └─────────────┬──────────────┘
                            │ paper_type (A1/B2/C3/R...)
                            │ type_confidence (0.0-1.0)
              ┌─────────────▼──────────────┐
              │  Stage 4: 差异化抽取        │  ← 新增
              │  按 paper_type 激活不同     │
              │  抽取器组合：               │
              │  A1-A4: DFT+Catalyst+WC   │
              │  B1-B3: 全部 7 个          │
              │  C1-C3: Elec+Mech+WC      │
              │  R: WC+Comprehensive      │
              └─────────────┬──────────────┘
                            │
              ┌─────────────▼──────────────┐
              │  Stage 5: 分类感知 RAG      │  ← 升级
              │  Retriever 按 paper_type   │
              │  过滤 + 证据偏好差异化      │
              │  Writer 按 paper_type      │
              │  切换章节模板              │
              └────────────────────────────┘
```

---

## 三、各 Stage 详细设计

### Stage 0：意图识别（新增）

**问题**：用户输入"CO2 电还原"时，系统不知道用户要的是 DFT 计算论文（A1）还是实验论文（C1），导致搜索结果混杂。

**方案**：在 AI 改写查询之前，增加一步轻量意图识别。

#### 3.0.1 数据模型

```python
class SearchIntent(BaseModel):
    """AI 自动查文献的意图识别结果"""
    target_types: list[str]       # 目标分类列表，如 ["A1", "B1"]
    domain_keywords: list[str]    # 领域关键词，如 ["CO2RR", "electrocatalysis"]
    methodology_hint: str         # 方法倾向，如 "computational" / "experimental" / "mixed" / "any"
    search_strategy: str           # 搜索策略，如 "broad" / "focused" / "review_first"
    confidence: float              # 意图识别置信度
```

#### 3.0.2 实现方案

**方案 A — LLM 快速识别（推荐）**：

- 复用现有 `_rewrite_ai_search_query` 的 LLM 调用通道
- 新增一个 system prompt，要求 LLM 返回 JSON 格式的 `SearchIntent`
- 与查询改写合并在一次 LLM 调用中完成（省一次 API 调用）
- temperature=0.05（极低随机性，确保分类稳定）
- **预期耗时**：+1~2 秒（与改写合并则零额外延迟）

**方案 B — 关键词规则匹配（降级方案）**：

- 维护一个 `INTENT_KEYWORD_MAP`：
  ```
  "DFT", "VASP", "Gaussian", "第一性原理" → A 类
  "原位", "operando", "XRD", "SEM" → C 类
  "综述", "review", "progress" → R 类
  ```
- 不调用 LLM，零延迟
- 用于 LLM 不可用时的降级

#### 3.0.3 Prompt 设计（方案 A 合并版）

```
你是一位材料科学文献检索专家。根据用户的自然语言查询，同时完成以下两项任务：

任务1 — 意图识别：
判断用户需要的论文类型（可多选）：
- A1-A4: 纯计算（催化机理/电子结构/高通量/MD）
- B1-B3: 计算+实验（电催化/储能/热催化）
- C1-C3: 纯实验（合成/器件/原位表征）
- R: 综述

任务2 — 查询改写：
将用户的自然语言查询转换为适合学术数据库检索的精确布尔查询式。

输出 JSON 格式：
{
  "target_types": ["A1", "B1"],
  "methodology_hint": "computational",
  "search_strategy": "focused",
  "rewritten_query": "CO2 electroreduction AND DFT AND (catalyst OR single-atom)"
}
```

#### 3.0.4 API 变更

`AISearchPayload` 新增可选字段：

```python
class AISearchPayload(BaseModel):
    query: str
    model: str = "deepseek-chat"
    max_results: int = 100
    skip_guard: bool = False
    providers: list[str] = []
    # --- 新增 ---
    target_types: list[str] = []          # 用户手动指定分类（覆盖 AI 识别）
    auto_intent: bool = True              # 是否启用意图识别（默认开启）
```

---

### Stage 1：分类感知查询改写（升级现有）

**当前实现**：`_rewrite_ai_search_query()` 只做"自然语言 → 学术查询"的通用改写，不感知分类。

**升级点**：

#### 3.1.1 改写 Prompt 注入领域词表

根据 `target_types`（来自 Stage 0 意图识别或用户手动指定），在改写 prompt 中注入领域专有词汇：

| target_type | 注入的领域词表 |
|---|---|
| A1 | DFT, VASP, transition state, NEB, SAC, DAC, reaction pathway |
| A2 | DOS, d-band center, Bader charge, charge density difference |
| A3 | high-throughput, screening, machine learning, descriptor |
| A4 | AIMD, molecular dynamics, diffusion, ionic transport |
| B1 | ORR, OER, HER, overpotential, Tafel, electrocatalysis |
| B2 | Li-S battery, specific capacity, cycling stability, rate performance |
| B3 | CO2RR, NRR, Sabatier principle, volcano plot |
| C1 | synthesis, hydrothermal, sol-gel, characterization |
| C2 | performance, capacity, coulombic efficiency, energy density |
| C3 | in-situ, operando, XAS, Raman, FTIR |
| R | review, progress, perspective, recent advances |

#### 3.1.2 改写策略差异化

| target_type | 改写策略 |
|---|---|
| A 类 | 偏重 methodology 关键词 + 计算软件名 |
| B 类 | 双重关键词（计算+实验），用 AND 连接 |
| C 类 | 偏重材料名 + 表征手段 + 性能指标 |
| R 类 | 添加 `review OR survey OR perspective` 限定词 |

#### 3.1.3 改写结果示例

| 用户输入 | 识别分类 | 改写结果 |
|---|---|---|
| "CO2电还原催化剂" | B1 | `CO2 electroreduction AND (catalyst OR electrocatalyst) AND (DFT OR "first-principles") AND (experimental OR "half-cell")` |
| "锂硫电池综述" | R | `("lithium-sulfur battery" OR "Li-S battery") AND (review OR survey OR progress OR perspective)` |
| "单原子催化剂DFT" | A1 | `(SAC OR "single-atom catalyst") AND (DFT OR VASP OR "first-principles") AND (reaction mechanism OR pathway OR "transition state")` |

---

### Stage 2：分类感知搜索（升级现有）

**当前实现**：`DiscoveryService.search()` 对所有 provider 均分配额，不感知分类。

**升级点**：

#### 3.2.1 搜索源优先级

不同分类的论文在不同数据库中的覆盖度不同：

| 分类 | 主力源 | 辅助源 | 原因 |
|---|---|---|---|
| A1-A4 | **arXiv**（60%） | OpenAlex（40%） | 计算论文大量预印本 |
| B1-B3 | **OpenAlex**（60%） | arXiv（30%）+ PubMed（10%） | 混合论文多在期刊发表 |
| C1-C3 | **OpenAlex**（50%） | PubMed（30%）+ Semantic Scholar（20%） | 实验论文偏期刊/医学 |
| R | **OpenAlex**（80%） | Semantic Scholar（20%） | 综述以期刊为主，可按 type 筛选 |

#### 3.2.2 OpenAlex 按类型筛选

OpenAlex API 支持 `type` 参数过滤。R 类可直接加 `&type=review`。

#### 3.2.3 搜索结果预分类

搜索结果返回后、下载前，对每条结果做**轻量分类预估**：

- 有 DOI 且期刊影响因子高 → 可能是 B/C 类
- 来源是 arXiv → 大概率 A 类
- 标题含 "review"/"progress"/"perspective" → 可能 R 类
- 标题含 "DFT"/"first-principles"/"ab initio" → 可能 A 类

预分类用于**决定是否下载 PDF**——如果用户只要 A 类，C 类论文可以直接跳过下载，仅保存元数据。

---

### Stage 3：入库 + 快速分类（升级现有）

**当前实现**：`ingest_pdf()` → `run_stage2()` 全量跑 7 个抽取器。

**升级点**：

#### 3.3.1 两阶段抽取管线

```
Stage 2a: 快速分类（轻量）
  └── 仅运行 ComprehensiveExtractor 的分类字段
      输入：abstract + 首段 markdown
      输出：paper_type + type_confidence
      耗时：~3-5 秒/篇
      LLM 调用：1 次（短 prompt，小 token）

Stage 2b: 差异化抽取（重量）
  └── 根据 paper_type 激活不同抽取器组合
      见 Stage 4 设计
```

#### 3.3.2 快速分类的输入优化

当前 `ComprehensiveExtractor` 处理全文（可能上万 token）。快速分类只需要：

- **abstract**（~200 词）
- **首段 markdown**（Introduction 前 500 词）
- **标题 + 作者 + 期刊**

总输入 ~700 词，LLM 输出只需要 `paper_type` + `type_confidence` + `brief_rationale`（3-5 词），总 token 消耗约 1/10。

#### 3.3.3 快速分类的数据模型

```python
class QuickClassificationResult(BaseModel):
    paper_type: str           # A1-A4 / B1-B3 / C1-C3 / R / Unknown
    type_confidence: float    # 0.0 - 1.0
    rationale: str            # 3-5 词分类理由，如 "DFT study of ORR mechanism"
```

#### 3.3.4 快速分类与全量分析的关系

- 快速分类（Stage 2a）的结果存入 `Paper.paper_type` 和 `Paper.type_confidence` 新字段
- 全量综合分析（Stage 2b 的 ComprehensiveExtractor）会覆盖/更新这些字段（因为输入更完整）
- 如果全量分析也跑了，以全量分析为准；如果只跑了快速分类（如 metadata-only 的论文），用快速分类结果

---

### Stage 4：差异化抽取（新增核心逻辑）

**当前实现**：`ExtractionPipelineService.run_stage2()` 固定顺序跑全部 7 个抽取器，无论论文类型。

**升级点**：

#### 3.4.1 抽取器激活矩阵

| paper_type | DFTSettings | Catalyst | DFTResults | Electrochemical | Mechanism | WritingCard | Comprehensive(全量) |
|---|---|---|---|---|---|---|---|
| **A1** 催化机理 | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| **A2** 电子结构 | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ |
| **A3** 高通量 | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| **A4** MD | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **B1** 电催化 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **B2** 储能 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **B3** 热催化 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| **C1** 新材料 | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| **C2** 器件性能 | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| **C3** 原位表征 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| **R** 综述 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **Unknown** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅（当前行为，全量跑） |

#### 3.4.2 LLM 调用节省估算

以 100 篇论文、A/B/C/R 各 25 篇为例：

| 场景 | 当前 LLM 调用次数 | 优化后 LLM 调用次数 | 节省率 |
|---|---|---|---|
| DFTSettingsExtractor | 100 | 75 (A+B) | 25% |
| DFTResultsExtractor | 100 | 75 (A+B) | 25% |
| CatalystExtractor | 100 | 75 (A1+A3+B) | 25% |
| ElectrochemicalExtractor | 100 | 75 (B+C) | 25% |
| MechanismExtractor | 100 | 75 (B1+B3+C3) | 25% |
| **总计** | **700** | **525** | **25%** |

加上快速分类省下的 Comprehensive 全量调用，综合节省约 **30-35%**。

#### 3.4.3 WritingCard 分类统一

当前 WritingCard 的 `paper_type` 只有 4 类（computational / experimental / mixed / review），与 Comprehensive 的 11 类没有映射。

**统一方案**：

```python
def paper_type_to_writing_card_type(paper_type: str) -> str:
    """将细粒度分类映射到 WritingCard 的粗粒度分类"""
    if paper_type.startswith("A"):
        return "computational"
    elif paper_type.startswith("B"):
        return "mixed"
    elif paper_type.startswith("C"):
        return "experimental"
    elif paper_type == "R":
        return "review"
    return "unknown"
```

- WritingCard 的 `paper_type` 改为从 Comprehensive 的分类**派生**，不再独立判断
- 细粒度分类（A1/B2/C3...）作为 source of truth
- WritingCard 保留 `paper_type` 字段用于写作模板匹配，但值由映射产生

---

### Stage 5：分类感知 RAG（升级现有）

#### 3.5.1 Retriever 分类过滤

**当前**：`Retriever.retrieve()` 接受 `paper_ids` 参数做白名单过滤，但不按分类筛选。

**升级**：

```python
def retrieve(
    self,
    query: str,
    paper_ids: list[UUID] | None = None,
    paper_type_filter: list[str] | None = None,  # ← 新增
    limit_per_type: int = 5,
) -> dict[str, list[dict[str, Any]]]:
```

`paper_type_filter` 的过滤逻辑：
- `["A"]` → 匹配所有 A 类（A1-A4）
- `["B1", "B2"]` → 精确匹配 B1 和 B2
- `None` → 不过滤（当前行为）

SQL 层过滤：
```python
# 在 _retrieve_sections / _retrieve_dft_results 等方法中
if paper_type_filter:
    query = query.join(Paper).where(
        Paper.paper_type.in_(
            _expand_type_filter(paper_type_filter)  # ["A"] → ["A1","A2","A3","A4"]
        )
    )
```

#### 3.5.2 证据偏好差异化

不同分类的论文，RAG 检索时应偏好不同类型的证据：

| 目标分类 | 偏好证据类型 | 权重调整 |
|---|---|---|
| A 类 | dft_results > mechanism_claims > sections | dft_results ×1.5 |
| B 类 | 各类型均衡 | 默认权重 |
| C 类 | electrochemical_performance > sections > mechanism_claims | electrochemical ×1.5 |
| R 类 | writing_cards > sections | writing_cards ×2.0 |

实现方式：在 `Retriever.retrieve()` 的打分阶段，根据 `paper_type_filter` 调整各类证据的 `limit_per_type`：

```python
EVIDENCE_BIAS = {
    "A": {"dft_results": 1.5, "electrochemical_performance": 0.5},
    "C": {"electrochemical_performance": 1.5, "dft_results": 0.5},
    "R": {"writing_cards": 2.0, "dft_results": 0.3, "mechanism_claims": 0.3},
}
```

#### 3.5.3 type_confidence 加权

`type_confidence` 高的论文，其证据在检索排序中应更受信任：

```python
# 在 _global_dedup 或评分阶段
confidence_boost = 1.0 + (paper.type_confidence - 0.5) * 0.4  # 置信度 0.5→1.0, 1.0→1.2
score *= confidence_boost
```

#### 3.5.4 Writer 分类适配模板

**当前**：`Writer.write()` 的 `sections` 参数默认为 `["outline", "introduction", "dft_results", "discussion", "figure_storyline"]`，对 A/C/R 类都不合适。

**升级**：根据目标论文分类切换默认章节列表和 prompt 模板：

| 目标分类 | 默认章节 | 差异化 prompt 重点 |
|---|---|---|
| A1-A4 | outline → introduction → computational_methods → results → discussion → figure_storyline | Methods 突出计算设置、软件、泛函；Results 突出能垒/电荷/DOS |
| B1-B3 | outline → introduction → methods → computational_results → experimental_validation → discussion → figure_storyline | 双重 Methods（计算+实验）；结果分计算和实验两段 |
| C1-C3 | outline → introduction → experimental_methods → results → discussion → figure_storyline | Methods 突出合成路线/表征手段；Results 突出性能指标 |
| R | outline → introduction → background → recent_progress → challenges → outlook → figure_storyline | 无 Methods；按主题分小节而非按实验/计算分 |

**实现方式**：

1. `Writer.write()` 新增 `target_paper_type: str | None = None` 参数
2. 根据 `target_paper_type` 选择不同的默认 `sections` 列表
3. `PaperWriterPromptBuilder.build()` 注入分类相关的 prompt 片段
4. 新增 `PAPER_TYPE_SECTION_TEMPLATES` 配置字典

---

## 四、前端升级设计

### 4.1 文献列表：分类标签 + 筛选

**当前**：文献列表只显示序号、标题、年份、状态标签。

**升级**：

| 元素 | 设计 | 位置 |
|---|---|---|
| 分类标签 | 彩色 pill 徽章：A 类蓝色、B 类绿色、C 类橙色、R 类紫色 | 标题右侧 |
| 置信度条 | 2px 高的色条（绿→黄→红），宽度 = confidence × 100% | 分类标签下方 |
| 筛选下拉 | `<select>` 下拉框：全部 / A-纯计算 / B-计算+实验 / C-纯实验 / R-综述 | 工具栏区域 |
| 统计饼图 | 库内 A/B/C/R 分布，点击扇区筛选 | 侧边或工具栏 |

**配色方案**（参考用户对可见性的要求，避免白色/浅色）：

| 分类 | 背景色 | 文字色 |
|---|---|---|
| A 类 | `#1a237e`（深蓝） | `#e8eaf6`（浅蓝白） |
| B 类 | `#1b5e20`（深绿） | `#e8f5e9`（浅绿白） |
| C 类 | `#e65100`（深橙） | `#fff3e0`（浅橙白） |
| R 类 | `#4a148c`（深紫） | `#f3e5f5`（浅紫白） |

### 4.2 论文详情：差异化标签页

**当前**：5 个标签页对所有论文展示相同内容。

**升级**：根据 `paper_type` 动态调整标签页内容和优先级：

| 标签页 | A 类显示 | B 类显示 | C 类显示 | R 类显示 |
|---|---|---|---|---|
| 论文详情 | 元数据 + 计算参数摘要 | 元数据 + 混合参数摘要 | 元数据 + 实验参数摘要 | 元数据 + 综述范围 |
| 内部AI整理 | 侧重 DFT 结果 | 侧重计算+实验互证 | 侧重性能数据 | 侧重综述逻辑 |
| 外部AI审核 | 同上 | 同上 | 同上 | 同上 |
| AI检索入库 | 不变 | 不变 | 不变 | 不变 |
| 聚合视图 | 不变 | 不变 | 不变 | 不变 |

### 4.3 AI 搜索入口：分类意图输入

**当前**：AI 搜索标签页只有一个文本输入框。

**升级**：

```html
<!-- 新增：分类意图选择器 -->
<div class="intent-selector">
  <label>论文类型偏好：</label>
  <select id="aiSearchTargetType">
    <option value="">自动识别（推荐）</option>
    <option value="A">A - 纯计算论文</option>
    <option value="B">B - 计算+实验论文</option>
    <option value="C">C - 纯实验论文</option>
    <option value="R">R - 综述论文</option>
  </select>
</div>
```

选择后，`runAISearch()` 和 `runAIWorkflow()` 将 `target_types` 传入 API 请求。

### 4.4 写作入口：目标分类选择

AI 写作面板（如存在）新增目标分类选择器，选择后自动切换章节模板和证据偏好。

---

## 五、数据库模型变更

### 5.1 Paper 模型新增字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `paper_type` | `String(20)` | `null` | 细粒度分类（A1/B2/C3/R...） |
| `type_confidence` | `Float` | `null` | 分类置信度 0.0-1.0 |
| `classification_source` | `String(20)` | `null` | 分类来源：`quick`（快速分类）/ `full`（全量分析）/ `manual`（用户手动） |

### 5.2 迁移策略

- 已有论文：`paper_type` 和 `type_confidence` 为 `null`，表示未分类
- 后续入库论文：Stage 2a 自动填充
- 已有论文的补充分类：提供"批量分类"后台任务（遍历所有未分类论文，只跑 Stage 2a）

### 5.3 索引

```sql
CREATE INDEX idx_paper_type ON paper(paper_type);
CREATE INDEX idx_paper_type_confidence ON paper(type_confidence);
```

用于 `paper_type_filter` 的高效过滤查询。

### 5.4 FigureDataPoint 模型（Phase 6 新增）

> ⚠️ **v1.3a 补充**（Gemini 第二轮审核发现遗漏）：原方案在 §12.4.3 和 Phase 6 提到了 `FigureDataPoint`，但第五章数据库模型变更中完全遗漏了其表结构定义。以下为补充。

采用**独立新表**方案（推荐，便于结构化 SQL 查询和 RAG 精确检索）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | `UUID` | `uuid4()` | 主键 |
| `figure_id` | `UUID` | - | 外键关联 `paper_figures.id`（CASCADE） |
| `paper_id` | `UUID` | - | 外键关联 `papers.id`（CASCADE），便于按论文查询 |
| `metric_name` | `String(255)` | - | 提取的指标名（如 `overpotential`、`tafel_slope`） |
| `metric_value` | `Float` | - | 数值（如 150.0、45.0） |
| `unit` | `String(64)` | `null` | 单位（如 `mV`、`mV dec⁻¹`） |
| `conditions` | `JSON` | `null` | 提取的实验条件（如 `{"electrolyte": "0.1 M KOH"}`） |
| `sample_label` | `String(128)` | `null` | 图例中对应的样品名（用于关联 CatalystSample） |
| `confidence` | `Float` | `1.0` | 提取置信度 0.0-1.0 |
| `raw_text` | `Text` | `null` | VLM 原始输出（用于审计） |

SQLAlchemy 模型定义：

```python
class FigureDataPoint(Base):
    __tablename__ = "figure_data_points"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    figure_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("paper_figures.id", ondelete="CASCADE"), index=True)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str] = mapped_column(sa.String(255))
    metric_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    conditions: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    sample_label: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)
    raw_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
```

索引：

```sql
CREATE INDEX idx_fdp_paper_id ON figure_data_points(paper_id);
CREATE INDEX idx_fdp_metric_name ON figure_data_points(metric_name);
```

迁移说明：
- 此表与 `paper_type` 三个字段分开迁移——`FigureDataPoint` 表在 Phase 6（图片 Level 3）实施时创建，不在前置 0b 中
- 迁移脚本命名为 `patch_add_figure_data_point.py`
- 同样需要提供 `downgrade.sql`（`DROP TABLE figure_data_points`）

> 备选方案（作为 `PaperFigure` 的 JSON 列）：在 `PaperFigure` 新增 `extracted_data` JSON 列存储数据点数组。优点是减少 JOIN，缺点是不利于 SQL 精确查询。当前推荐独立新表方案。

---

## 六、API 变更汇总

### 6.1 新增端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/papers/classify-batch` | POST | 批量分类已有论文（仅 Stage 2a） |
| `/api/papers/stats/by-type` | GET | 按分类统计论文数量 |

### 6.2 修改端点

| 端点 | 变更 |
|---|---|
| `POST /api/papers/ai_search` | 请求新增 `target_types`、`auto_intent` 字段 |
| `POST /api/papers/ai_workflow` | 同上 |
| `POST /api/papers/ai_workflow/jobs` | 同上 |
| `GET /api/papers` | 新增 `paper_type` 查询参数（筛选分类） |
| `GET /api/papers/{id}` | 响应新增 `paper_type`、`type_confidence`、`classification_source` 字段 |
| `POST /api/rag/write` | 请求新增 `target_paper_type` 字段 |

### 6.3 响应字段新增

`AISearchResponse` 新增：
```python
intent: SearchIntent | None        # 意图识别结果
```

`AIWorkflowResponse` 新增：
```python
classified_count: int              # 成功分类的论文数
skipped_by_type: int               # 因分类不符跳过的下载数
```

---

## 七、与现有 AI 自动查文献的印证关系

### 7.1 原方案保留部分

| 原方案要素 | 在新方案中的位置 | 变化 |
|---|---|---|
| LLM 改写查询 | Stage 1（升级） | 注入领域词表 + 分类感知 |
| 多源并发检索 | Stage 2（升级） | 按分类调 provider 优先级 |
| 下载即入库 | Stage 3（保留） | 无变化，核心原则不变 |
| CitationGuard 护栏 | Stage 5（保留） | 无变化，继续使用 |
| 后台任务机制 | 全局（保留） | 无变化 |
| 去重机制 | Stage 2（保留） | 无变化 |
| 元数据补全 | Stage 3（保留） | 无变化 |

### 7.2 原方案扩展部分

| 新增要素 | 原方案无 | 新方案位置 |
|---|---|---|
| 意图识别 | ❌ 无 | Stage 0 |
| 分类感知改写 | ❌ 通用改写 | Stage 1 |
| 搜索源优先级 | ❌ 均分 | Stage 2 |
| 搜索结果预分类 | ❌ 无 | Stage 2 |
| 两阶段抽取 | ❌ 全量跑 | Stage 3-4 |
| 差异化抽取矩阵 | ❌ 无 | Stage 4 |
| WritingCard 分类统一 | ❌ 两套独立 | Stage 4 |
| Retriever 分类过滤 | ❌ 无 | Stage 5 |
| 证据偏好差异化 | ❌ 无 | Stage 5 |
| Writer 分类模板 | ❌ 单一模板 | Stage 5 |
| 前端分类标签/筛选 | ❌ 无 | 前端 |
| 分类意图输入 | ❌ 无 | 前端 |

---

## 八、实施路线图

### Phase 1：基础分类能力（2-3 天）

**目标**：让论文有分类标签，能在前端看到和筛选。

| 任务 | 涉及文件 | 工作量 |
|---|---|---|
| Paper 模型新增 paper_type/type_confidence/classification_source 字段 | `db/models.py` + Alembic 迁移 | 0.5 天 |
| ExtractionPipeline.run_stage2 改为两阶段：先快速分类，再差异化抽取 | `extraction_pipeline.py` | 1 天 |
| 前端分类标签 + 筛选下拉 | `index.html` | 0.5 天 |
| GET /api/papers 新增 paper_type 查询参数 | `api/papers.py` | 0.5 天 |

**交付标准**：
- 新入库论文自动分类并显示标签
- 文献列表可按 A/B/C/R 筛选
- 已有论文显示"未分类"

### Phase 2：分类感知搜索（2-3 天）

**目标**：AI 查文献时能感知分类意图，搜索更精准。

| 任务 | 涉及文件 | 工作量 |
|---|---|---|
| 意图识别（合并到查询改写中） | `api/papers.py::_rewrite_ai_search_query()` | 1 天 |
| AISearchPayload/AIWorkflowPayload 新增 target_types | `schemas/api.py` | 0.5 天 |
| 改写 prompt 注入领域词表 | `api/papers.py` | 0.5 天 |
| 搜索源优先级（按分类分配配额） | `discovery_service.py` | 1 天 |
| 前端分类意图选择器 | `index.html` | 0.5 天 |

**交付标准**：
- 用户可选"我只要 A 类论文"，搜索结果偏向计算论文
- 不选则自动识别意图
- LLM 不可用时降级为通用搜索

### Phase 3：分类感知 RAG + 写作（2-3 天）

**目标**：AI 写作时根据目标分类切换模板和证据偏好。

| 任务 | 涉及文件 | 工作量 |
|---|---|---|
| Retriever 新增 paper_type_filter 参数 | `rag/retriever.py` | 0.5 天 |
| 证据偏好差异化（EVIDENCE_BIAS 配置） | `rag/retriever.py` | 0.5 天 |
| type_confidence 加权 | `rag/retriever.py` | 0.5 天 |
| Writer 分类模板（PAPER_TYPE_SECTION_TEMPLATES） | `rag/writer.py` + `rag/prompt_builder.py` | 1 天 |
| WritingCard 分类统一映射 | `extractors/writing_card_extractor.py` + `extraction_pipeline.py` | 0.5 天 |

**交付标准**：
- 写 A 类论文时只检索 A 类证据，章节模板偏计算
- 写 C 类论文时证据偏实验，章节模板偏性能
- WritingCard 的 paper_type 由综合分析派生

### Phase 4：存量补充分类 + Polish（1-2 天）

| 任务 | 涉及文件 | 工作量 |
|---|---|---|
| 批量分类后台任务 | `api/papers.py` 新增端点 | 0.5 天 |
| 分类统计端点 + 前端饼图 | `api/papers.py` + `index.html` | 0.5 天 |
| 分栏拖拽重建 | `index.html::initSplitDrag` | 0.5 天 |
| 桌面端适配（分类标签展示） | `search_page.py` + `library_page.py` | 0.5 天 |

---

## 九、风险与缓解

| 风险 | 可能性 | 影响 | 缓解措施 |
|---|---|---|---|
| LLM 分类不准（A/B 混淆） | 中 | 抽取器组合错误，漏跑或少跑 | Unknown 类型走全量抽取兜底；`type_confidence < 0.6` 也走全量 |
| 意图识别误判 | 中 | 搜索结果偏移 | 用户可手动指定 target_types 覆盖；LLM 不可用时降级 |
| 快速分类与全量分析结果不一致 | 低 | 分类标签闪烁（快速=A1，全量=B1） | 以全量为准；前端展示"待确认"状态直到全量分析完成 |
| 前端改动量大（index.html 1795 行） | 中 | 代码可维护性 | 按最小改动原则，只加不删；新增代码独立为函数 |
| 数据库迁移 | 低 | 已有数据 paper_type 为 null | null 视为 Unknown，不影响现有功能 |

---

## 十、验收标准

### 功能验收

- [ ] 新入库论文自动分类，前端显示分类标签
- [ ] 文献列表支持按 A/B/C/R 筛选
- [ ] AI 搜索支持"我只要 X 类论文"，搜索结果偏移
- [ ] A 类论文不跑 ElectrochemicalExtractor，C 类不跑 DFTSettingsExtractor
- [ ] RAG 写 A 类论文时只用 A 类证据
- [ ] Writer 写 R 类论文时使用综述模板（无 Methods 章节）
- [ ] 已有论文可通过"批量分类"补充分类
- [ ] 桌面端同步后可见分类标签

### 性能验收

- [ ] 快速分类（Stage 2a）< 5 秒/篇
- [ ] 差异化抽取比全量抽取节省 ≥ 25% LLM 调用
- [ ] Retriever 加 paper_type_filter 后查询延迟不增加

### 质量验收

- [ ] A 类论文的 DFT 结果抽取不因分类而被跳过
- [ ] type_confidence < 0.6 的论文走全量抽取
- [ ] Unknown 类型走全量抽取（与当前行为一致）

---

## 十一、附录

### A. 关键文件速查

| 层 | 文件 | 职责 |
|---|---|---|
| API | `literature-ai/backend/app/api/papers.py` | AI 搜索/workflow 端点 |
| Schema | `literature-ai/backend/app/schemas/api.py` | 请求/响应数据模型 |
| 搜索 | `literature-ai/backend/app/services/discovery_service.py` | 多源检索+去重 |
| 入库 | `literature-ai/backend/app/services/paper_ingestion.py` | PDF 入库+解析 |
| 抽取管线 | `literature-ai/backend/app/services/extraction_pipeline.py` | Stage 2 抽取调度 |
| 综合分析 | `literature-ai/backend/app/extractors/comprehensive_extractor.py` | 分类+综合分析 |
| DFT 设置 | `literature-ai/backend/app/extractors/dft_settings_extractor.py` | DFT 计算参数 |
| 催化剂 | `literature-ai/backend/app/extractors/catalyst_extractor.py` | 催化剂样品 |
| DFT 结果 | `literature-ai/backend/app/extractors/dft_results_extractor.py` | 吸附能/能垒等 |
| 电化学 | `literature-ai/backend/app/extractors/electrochemical_performance_extractor.py` | 电化学性能 |
| 机理 | `literature-ai/backend/app/extractors/mechanism_extractor.py` | 机理声明 |
| 写作卡 | `literature-ai/backend/app/extractors/writing_card_extractor.py` | 写作卡片 |
| RAG 检索 | `literature-ai/backend/app/rag/retriever.py` | 混合检索 |
| RAG Prompt | `literature-ai/backend/app/rag/prompt_builder.py` | Prompt 构建 |
| RAG 写作 | `literature-ai/backend/app/rag/writer.py` | 生成+护栏 |
| RAG 护栏 | `literature-ai/backend/app/rag/citation_guard.py` | 引用验证 |
| 数据模型 | `literature-ai/backend/app/db/models.py` | Paper/DFTResult 等 |
| 前端 | `literature-ai/frontend/pages/literature_library/index.html` | 统一工作台 |
| 桌面搜索 | `app/ui/search_page.py` | AI 自动查文献按钮 |
| 桌面库 | `app/ui/library_page.py` | 同步 LitAI 结果 |
| 桌面抽取 | `app/ui/extraction_page.py` | AI 抽取展示 |
| LitAI 客户端 | `app/services/literature_ai_client.py` | HTTP 客户端 |

### B. 分类体系速查

| 代码 | 名称 | 典型关键词 | 典型期刊 |
|---|---|---|---|
| A1 | 纯计算-催化机理 | SAC, DAC, reaction pathway, NEB | ACS Catal., J. Catal. |
| A2 | 纯计算-电子结构 | DOS, d-band, Bader, charge | J. Phys. Chem. C |
| A3 | 纯计算-高通量筛选 | high-throughput, ML, descriptor | NPJ Comput. Mater. |
| A4 | 纯计算-分子动力学 | AIMD, diffusion, ionic transport | Chem. Mater. |
| B1 | 计算+实验-电催化 | ORR, OER, HER, overpotential | Nat. Catal., Energy Environ. Sci. |
| B2 | 计算+实验-储能 | Li-S, capacity, cycling | Adv. Energy Mater. |
| B3 | 计算+实验-热催化 | CO2RR, NRR, Sabatier | ACS Catal. |
| C1 | 纯实验-新材料合成 | hydrothermal, sol-gel, characterization | Chem. Soc. Rev. |
| C2 | 纯实验-器件性能 | full cell, rate, coulombic eff. | Adv. Funct. Mater. |
| C3 | 纯实验-原位表征 | in-situ, operando, XAS, Raman | Nat. Commun. |
| R | 综述 | review, progress, perspective | Chem. Rev., Adv. Mater. |

### C. 搜索源能力对比

| 搜索源 | 免费额度 | 覆盖领域 | 计算论文 | 实验论文 | 综述 | PDF 下载 |
|---|---|---|---|---|---|---|
| OpenAlex | 完全免费 | 全领域 | ✅ | ✅✅ | ✅✅ | ❌（仅元数据） |
| arXiv | 完全免费 | 物理/CS/数学 | ✅✅ | ❌ | ❌ | ✅ |
| PubMed | 免费但有速率限制 | 生物医学 | ❌ | ✅✅ | ✅ | ❌ |
| Semantic Scholar | 免费（有速率限制） | 全领域 | ✅ | ✅ | ✅ | ✅（部分） |
| Crossref | 免费 | 全领域 | ✅ | ✅ | ✅ | ❌ |
| X-MOL | 免费 | 化学为主 | ✅ | ✅✅ | ✅ | ❌ |

---

> **文档版本**：v1.1 | **更新日期**：2026-05-23 | **适用范围**：literature-ai 子系统 + 桌面端
>
> v1.1 更新：新增「十二、论文图片抽取与智能解析」章节

## 十二、论文图片抽取与智能解析

### 12.0 现状诊断

| 维度 | 现状 | 问题 |
|---|---|---|
| **图片提取** | Docling 解析出 `pictures` 节点（含页面坐标和 caption 引用），但**不提取图片像素** | `storage/figures/` 始终为空 |
| **caption 解析** | Docling 返回 `$ref` 引用格式（如 `{"$ref": "#/texts/45"}`），当前代码直接 `item.get("caption")` 拿到 None，回退为 "Figure N" | **绝大多数图注丢失** |
| **GROBID 图片** | 请求参数含 `teiCoordinates: figure`，但 TEI XML 中的 `<figure>` 元素**未被解析** | 白白浪费 GROBID 的 figure 提取能力 |
| **image_path** | `PaperFigure.image_path` **始终为 None** | 前端无法展示图片 |
| **图片 AI 解析** | **完全不存在** | 化学结构式、相图、性能曲线等视觉信息完全丢失 |
| **前端展示** | 仅显示 caption 文字 + "路径: -" | 用户看不到任何实际图片 |
| **RAG 检索** | caption 通过 `PaperSection(figure_caption)` 间接参与检索 | 无图片 embedding，无多模态检索 |

**核心结论**：当前项目的"图片处理"实际上是**图片标题的文本处理**。图片本身从未被提取、存储、展示或分析。

---

### 12.1 图片抽取三阶段方案

论文图片的完整处理链分为三个阶段，对应三个层级的能力：

```
┌─────────────────────────────────────────────────────────────┐
│  Level 1: 图片提取与存储（基础）                                │
│  PDF → 截取图片区域 → 保存 PNG → PaperFigure.image_path      │
│  价值：用户能在前端"看到"论文图片                              │
│  依赖：Docling bbox 坐标 + pdf2image / PyMuPDF               │
├─────────────────────────────────────────────────────────────┤
│  Level 2: 图片分类与标注（增强）                               │
│  PNG → VLM 分类(结构式/相图/曲线/表征/示意) → figure_role     │
│  价值：每张图自动标注角色，可按类型筛选                        │
│  依赖：视觉语言模型（VLM），如 GPT-4o-mini / Qwen-VL        │
├─────────────────────────────────────────────────────────────┤
│  Level 3: 图片内容解析（深度）                                │
│  PNG → VLM/OCR → 结构化数据提取（数值/结构式/坐标轴）         │
│  价值：图中数据可检索、可对比、可写入 AI 生成内容              │
│  依赖：VLM + 领域 prompt + 后处理规则                        │
└─────────────────────────────────────────────────────────────┘
```

---

### 12.2 Level 1：图片提取与存储

#### 12.2.1 图片提取方案

**方案 A — Docling bbox + PyMuPDF 截取（推荐）**

Docling 解析结果中每个 picture 节点包含 `prov`（provenance）信息：

```json
{
  "prov": [{
    "page_no": 5,
    "bbox": {"l": 72.0, "t": 180.0, "r": 540.0, "b": 680.0}
  }],
  "captions": [{"$ref": "#/texts/45"}]
}
```

利用 `bbox` 坐标从 PDF 中截取对应区域：

```python
import fitz  # PyMuPDF

def extract_figure_from_pdf(pdf_path: str, page_no: int, bbox: dict) -> bytes:
    doc = fitz.open(pdf_path)
    page = doc[page_no - 1]  # 0-indexed
    rect = fitz.Rect(bbox["l"], bbox["t"], bbox["r"], bbox["b"])
    pix = page.get_pixmap(clip=rect, dpi=200)
    return pix.tobytes("png")
```

- 输出：PNG 二进制
- 存储路径：`storage/figures/{paper_serial}_{figure_index}.png`
- 写入 `PaperFigure.image_path`

**方案 B — PyMuPDF 全页面图片提取（降级方案）**

当 Docling 未提供 bbox 时，直接用 PyMuPDF 提取 PDF 中嵌入的所有图片对象：

```python
for page in doc:
    for img_index, img in enumerate(page.get_images(full=True)):
        xref = img[0]
        base_image = doc.extract_image(xref)
        # base_image["image"] 是 bytes
```

缺点：无法精确定位图片在文中的位置，且会提取到 logo、装饰图等无关图片。

#### 12.2.2 Caption 解析修复

当前 Docling 的 caption 是 `$ref` 引用格式，需要解析引用获取真实文本：

```python
def resolve_caption_ref(picture: dict, docling_json: dict) -> str:
    """解析 Docling $ref 引用，获取真实 caption 文本"""
    captions = picture.get("captions") or []
    for cap_ref in captions:
        ref_path = cap_ref.get("$ref", "")
        # 格式: "#/texts/45" → 从 docling_json["texts"] 中索引
        if ref_path.startswith("#/texts/"):
            idx = int(ref_path.split("/")[-1])
            texts = docling_json.get("texts", [])
            if idx < len(texts):
                return texts[idx].get("text", "").strip()
    # 降级：用 picture 的 label 字段
    return picture.get("label", "") or f"Figure {index}"
```

#### 12.2.3 GROBID Figure 补充解析

GROBID 的 TEI XML 中包含 `<figure>` 元素，应额外解析：

```xml
<figure xmlns="http://www.tei-c.org/ns/1.0" xml:id="fig_0">
  <head>Figure 1. Adsorption configurations of CO2 on Fe-N4.</head>
  <graphic url="fig0" mimeType="image/png"/>
</figure>
```

从 TEI 中提取：
- `<head>` → caption
- `@xml:id` → figure_id
- `<graphic @url>` → 图片标识（GROBID 不直接提取图片，但提供关联信息）

#### 12.2.4 数据模型变更

`PaperFigure` 新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `image_path` | Text | null | 实际图片文件路径（从 None → 有值） |
| `image_hash` | String(64) | null | 图片 SHA256 哈希（去重用） |
| `width` | Integer | null | 图片宽度（px） |
| `height` | Integer | null | 图片高度（px） |
| `extraction_method` | String(32) | null | 提取方式：`docling_bbox` / `pymupdf_embedded` / `pymupdf_clip` |

#### 12.2.5 前端展示

在论文详情的"图片"区域，从纯文本卡片升级为**缩略图网格**：

```html
<div class="figure-grid">
  <!-- 每张图 -->
  <div class="figure-card" onclick="showFigureModal(figureId)">
    <img src="/api/papers/{paper_id}/figures/{figure_id}/image" 
         loading="lazy" alt="Figure">
    <div class="figure-caption">吸附构型及结合能</div>
    <span class="figure-role-badge">结构式</span>
  </div>
</div>
```

新增 API：
- `GET /api/papers/{paper_id}/figures` — 列出图片元数据
- `GET /api/papers/{paper_id}/figures/{figure_id}/image` — 返回 PNG 二进制

---

### 12.3 Level 2：图片分类与标注

#### 12.3.1 图片角色分类体系

基于材料领域论文图片的常见类型，定义以下 `figure_role` 分类：

| figure_role | 中文名 | 典型内容 | 出现频率 |
|---|---|---|---|
| `crystal_structure` | 晶体结构 | 晶胞、原子构型、吸附位 | A/B 类极高 |
| `electronic_structure` | 电子结构 | DOS 图、能带图、电荷密度 | A 类极高 |
| `reaction_pathway` | 反应路径 | NEB 能垒图、反应坐标 | A1/B1 高 |
| `phase_diagram` | 相图 | 稳定性相图、Pourbaix 图 | A3 高 |
| `morphology` | 形貌表征 | SEM/TEM/AFM 图 | B/C 类极高 |
| `spectroscopy` | 光谱表征 | XRD/Raman/FTIR/XPS | B/C 类极高 |
| `electrochemistry` | 电化学曲线 | CV/LSV/Tafel/EIS | B1/B2 极高 |
| `performance` | 性能数据 | 循环寿命、倍率性能、容量 | B2/C2 极高 |
| `schematic` | 示意图 | 机理示意图、装置图 | 所有类常见 |
| `comparison` | 对比图 | 与文献对比的表格/柱状图 | 所有类常见 |
| `other` | 其他 | 照片、流程图等 | 低 |

#### 12.3.2 VLM 分类方案

**调用方式**：复用现有 `LLMService` 通道，但使用视觉模型（如 `gpt-4o-mini`、`qwen-vl-plus`）

**Prompt 设计**：

```
你是一位材料科学论文图表分类专家。分析这张论文图片，返回 JSON 格式的分类结果：

{
  "figure_role": "crystal_structure | electronic_structure | reaction_pathway | phase_diagram | morphology | spectroscopy | electrochemistry | performance | schematic | comparison | other",
  "role_confidence": 0.0-1.0,
  "content_summary": "一句话描述图片内容，如'Fe-N4单原子催化剂上CO2吸附的优化构型及吸附能'",
  "key_elements": ["Fe-N4", "CO2", "adsorption energy", "-1.23 eV"]
}

分类标准：
- crystal_structure: 原子/分子构型、晶胞、吸附位、缺陷结构
- electronic_structure: DOS图、能带、电荷密度差、Bader电荷可视化
- reaction_pathway: NEB能垒图、反应坐标、过渡态
- phase_diagram: 稳定性相图、Pourbaix图、凸包图
- morphology: SEM/TEM/AFM/HRTEM 等形貌图
- spectroscopy: XRD/Raman/FTIR/XPS/EXAFS 等光谱
- electrochemistry: CV/LSV/Tafel/EIS/恒流充放电曲线
- performance: 循环寿命/倍率/容量/效率等性能图
- schematic: 机理示意图、装置图、流程图
- comparison: 与其他工作对比的柱状图/雷达图/表格
```

**调用频率**：每张图 1 次 VLM 调用，一篇论文约 5-10 张图 → 5-10 次调用

**降级方案**：VLM 不可用时，回退到当前基于 caption 的 `_classify_figure_purpose` 规则匹配。

#### 12.3.3 图片分类与论文分类的关联

| 论文 paper_type | 高频 figure_role | 低频 figure_role |
|---|---|---|
| A1 催化机理 | crystal_structure, reaction_pathway, electronic_structure | morphology, performance |
| A2 电子结构 | electronic_structure, phase_diagram | electrochemistry, morphology |
| A3 高通量 | phase_diagram, crystal_structure, comparison | morphology, spectroscopy |
| A4 MD | crystal_structure, comparison | electrochemistry, performance |
| B1 电催化 | electrochemistry, crystal_structure, reaction_pathway | phase_diagram |
| B2 储能 | performance, electrochemistry, morphology | reaction_pathway, electronic_structure |
| B3 热催化 | crystal_structure, reaction_pathway, spectroscopy | electronic_structure, phase_diagram |
| C1 合成 | morphology, spectroscopy, schematic | electronic_structure, reaction_pathway |
| C2 器件性能 | performance, electrochemistry, morphology | crystal_structure, phase_diagram |
| C3 原位表征 | spectroscopy, electrochemistry, crystal_structure | phase_diagram |
| R 综述 | schematic, comparison | (全面但单类频率低) |

这个关联可用于**验证分类一致性**：如果一篇论文被分类为 A1，但图片全是 morphology 和 spectroscopy，可能分类有误。

---

### 12.4 Level 3：图片内容深度解析

#### 12.4.1 按图片类型的差异化解析

不同类型的图片，解析目标完全不同：

| figure_role | 解析目标 | 输出数据结构 |
|---|---|---|
| `crystal_structure` | 吸附位、配位环境、键长/键角 | `{sites: [...], bond_lengths: [...], coordination: "N4"}` |
| `electronic_structure` | DOS 峰位、d带中心值、带隙 | `{d_band_center: -1.23, band_gap: 0.0, peaks: [...]}` |
| `reaction_pathway` | 各步能垒、决速步 | `{steps: [...], rate_limiting_step: "TS2", barriers: [...]}` |
| `phase_diagram` | 稳定相区域、临界点 | `{stable_regions: [...], critical_points: [...]}` |
| `electrochemistry` | 过电位、电流密度、Tafel斜率 | `{onset_potential: 0.85, current_density: 10.2, tafel_slope: 68}` |
| `performance` | 容量、循环次数、保持率 | `{capacity: 1200, cycles: 500, retention: 89.3}` |
| `spectroscopy` | 特征峰位置、相组成 | `{peaks: [...], phases: [...]}` |
| `morphology` | 颗粒尺寸、形貌描述 | `{particle_size: "50nm", shape: "nanosphere", uniformity: "high"}` |

#### 12.4.2 VLM 数值提取方案

**Prompt 模板**（以 electrochemistry 为例）：

```
你是一位电化学数据分析专家。从这张电化学曲线图中提取关键数值。

请返回 JSON 格式：
{
  "data_points": [
    {"metric": "onset_potential", "value": 0.85, "unit": "V vs RHE", "confidence": 0.9},
    {"metric": "current_density_at_10mA", "value": 1.52, "unit": "V vs RHE", "confidence": 0.85},
    {"metric": "tafel_slope", "value": 68, "unit": "mV/dec", "confidence": 0.8}
  ],
  "axis_labels": {"x": "Potential (V vs RHE)", "y": "Current density (mA/cm²)"},
  "legend_entries": ["Fe-N4/C", "Fe-N4", "Pt/C"],
  "notes": "LSV曲线，扫描速率5mV/s"
}

注意事项：
- 只提取图中可明确读出的数值，不确定的给低 confidence
- 坐标轴标签必须记录（用于后续数据对比）
- 图例中的样品名必须记录（用于关联具体催化剂）
```

#### 12.4.3 数据存储与关联

提取的结构化数据存入新的 `FigureDataPoint` 表：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `figure_id` | UUID | 外键关联 `paper_figures` |
| `metric` | String(128) | 指标名（如 `onset_potential`） |
| `value` | Float | 数值 |
| `unit` | String(64) | 单位（如 `V vs RHE`） |
| `confidence` | Float | 提取置信度 |
| `sample_label` | String(128) | 图例中对应的样品名 |
| `raw_text` | Text | VLM 原始输出（用于审计） |

`FigureDataPoint` 与 `DFTResult` / `ElectrochemicalPerformance` 的关系：

- VLM 从图中提取的数据与从文本中提取的数据**互补**
- 通过 `sample_label` 关联到 `CatalystSample`
- 汇总到综合分析时，文字+图片数据合并去重

#### 12.4.4 图片数据进入 RAG

**新增 EvidenceSpan 类型**：`"figure_data"`

- 每个 `FigureDataPoint` 生成一条 EvidenceSpan
- `object_type = "figure_data"`
- `text = f"{metric}: {value} {unit} (from Figure {index})"`
- `embedding` 用 metric + value + unit 的文本编码
- `figure` 字段记录来源图 ID

检索时，`figure_data` 类型证据与 `dft_results`、`electrochemical_performance` 并列，可被 Retriever 检索到。

**Writer 消费**：当 AI 写作需要"CO2RR 过电位数据"时，既可以从文字描述中检索，也可以从图片提取的数值中检索，双重证据源提高覆盖度。

---

### 12.5 图片抽取与分级知识库的整合

图片处理与前面"分类感知全链路"的整合点：

| Stage | 图片相关升级 |
|---|---|
| Stage 2 搜索 | 无直接影响 |
| Stage 3 入库 | 入库时自动执行 Level 1（图片提取+存储），可选执行 Level 2（VLM 分类） |
| Stage 4 差异化抽取 | 按论文分类选择性执行 Level 3（A 类优先解析 crystal_structure/electronic_structure，C 类优先解析 morphology/spectroscopy） |
| Stage 5 RAG+写作 | figure_data 作为新证据类型参与检索；Writer 引用图片时自动插入 `Figure X` 标记 |

**差异化图片解析矩阵**：

| paper_type | Level 2 必跑角色 | Level 3 必跑角色 | 跳过角色 |
|---|---|---|---|
| A1 | crystal_structure, electronic_structure, reaction_pathway | 全部 3 个 | morphology, spectroscopy |
| A2 | electronic_structure, phase_diagram | 全部 2 个 | morphology, electrochemistry |
| A3 | phase_diagram, crystal_structure, comparison | phase_diagram, crystal_structure | morphology, spectroscopy |
| A4 | crystal_structure, comparison | crystal_structure | electrochemistry, performance |
| B1 | electrochemistry, crystal_structure, reaction_pathway | electrochemistry, reaction_pathway | phase_diagram |
| B2 | performance, electrochemistry, morphology | performance, electrochemistry | reaction_pathway, electronic_structure |
| B3 | crystal_structure, reaction_pathway, spectroscopy | reaction_pathway | electronic_structure, phase_diagram |
| C1 | morphology, spectroscopy, schematic | spectroscopy | electronic_structure, reaction_pathway |
| C2 | performance, electrochemistry, morphology | performance, electrochemistry | crystal_structure, phase_diagram |
| C3 | spectroscopy, electrochemistry, crystal_structure | spectroscopy | phase_diagram |
| R | schematic, comparison | (不跑 Level 3) | (除 schematic/comparison 外) |

---

### 12.6 实施优先级与路线

| 优先级 | 任务 | 层级 | 工作量 | 价值 |
|---|---|---|---|---|
| **P0** | 修复 caption 解析（`$ref` 引用 → 真实文本） | Level 1 | 0.5 天 | 当前几乎所有图注丢失，修复后所有下游功能受益 |
| **P0** | 图片提取与存储（Docling bbox + PyMuPDF 截取） | Level 1 | 1 天 | 用户能在前端看到论文图片 |
| **P0** | 前端图片缩略图展示 | Level 1 | 0.5 天 | 直接可见的用户体验提升 |
| **P1** | 图片 API（列表 + 二进制下载） | Level 1 | 0.5 天 | 前端展示的基础 |
| **P1** | VLM 图片角色分类（Level 2） | Level 2 | 1 天 | 图片可按类型筛选 |
| **P1** | 前端图片筛选（按 figure_role） | Level 2 | 0.5 天 | 用户可只看结构式/只看曲线 |
| **P2** | VLM 数值提取（Level 3，electrochemistry 优先） | Level 3 | 2 天 | 图中数据可检索 |
| **P2** | FigureDataPoint 模型 + EvidenceSpan | Level 3 | 1 天 | 图片数据进入 RAG |
| **P2** | 按论文分类差异化图片解析 | Level 3 | 0.5 天 | 省 VLM 调用 |
| **P3** | 其他图片类型的 Level 3 解析 | Level 3 | 1-2 天/类型 | 逐步扩展 |

**建议实施顺序**：先做 P0 的三个任务（caption 修复 + 图片提取 + 前端展示），让用户能看到图片。然后 P1 的 VLM 分类让图片可筛选。P2 的数值提取是锦上添花。

---

### 12.7 风险与缓解

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| Docling bbox 坐标不准确 | 中 | 截取的图片偏移或残缺 | 允许 bbox 向外扩展 5-10% 的 padding；失败时降级到 PyMuPDF 全图提取 |
| VLM 分类不准（schematic vs comparison 混淆） | 中 | 图片标签错误 | confidence < 0.7 的标记为"待确认"；允许用户手动修正 |
| VLM 数值提取精度有限 | 高 | 提取的数值与实际有偏差 | 只用于辅助参考，不直接作为引用依据；confidence 标注让用户判断 |
| VLM 调用成本 | 中 | 每篇论文 5-10 张图 × VLM | Level 2 只需 1 次调用/图（~0.01 美元/图），Level 3 可按需执行 |
| 图片版权 | 低 | 存储论文原始图片 | 仅用于个人研究，不对外分发；API 需认证 |

---

### 12.8 验收标准

- [ ] 新入库论文的图片自动提取保存到 `storage/figures/`
- [ ] `PaperFigure.image_path` 不再为 None
- [ ] `PaperFigure.caption` 显示真实图注而非 "Figure N"
- [ ] 前端论文详情页展示图片缩略图（可点击放大）
- [ ] VLM 分类后，图片带有 role 标签（结构式/曲线/表征等）
- [ ] 前端可按 figure_role 筛选图片
- [ ] Level 3 完成后，图中数值可通过 RAG 检索到

---

> **文档版本**：v1.2 | **更新日期**：2026-05-23 | **适用范围**：literature-ai 子系统 + 桌面端
>
> v1.2 更新：整合 Codex 代码审查结论（第十三章），修正 3 个硬性缺口 + 4 个设计偏差 + 7 个遗漏点，调整实施路线图

## 十三、Codex 代码审查结论与修正

> 基于 Codex 对本方案 v1.1 与实际代码的逐行对照审查，逐条分析并给出修正方案。
> 审查结论经源码二次验证，全部采纳。

---

### 13.1 🔴 硬性前提缺口（不解决 = Phase 1 无法启动）

#### 缺口 C1：`Paper` 模型缺少 `paper_type`/`type_confidence`/`classification_source` 三个字段

**Codex 诊断**：`models.py` 第 59-86 行确认，`Paper` 类只有 `comprehensive_analysis`（JSON blob），分类数据埋在 blob 中无法被 SQL 索引或 WHERE 过滤。

**源码验证**：✅ 确认。第 84 行 `comprehensive_analysis: Mapped[dict | None] = mapped_column(json_type(), nullable=True)` 是唯一的分类存储位置。

**额外发现**：`requirements.txt` 第 5 行有 `alembic==1.16.1`，但项目**从未实际使用**（无 `alembic/` 目录、无 `alembic.ini`），表结构完全靠 `create_all` 创建。历史上添加 `serial_number` 字段时已用 `patch_papers.py` backfill 绕过。

**修正方案**：

1. 在 `Paper` 模型中新增三个字段：

```python
# models.py Paper 类新增
paper_type: Mapped[str | None] = mapped_column(sa.String(16), nullable=True, index=True)
type_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
classification_source: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
```

2. 迁移策略（无 Alembic 的 SQLite/PostgreSQL 双路方案）：

```python
# patch_add_paper_type.py
"""Backfill paper_type/type_confidence/classification_source from comprehensive_analysis JSON."""

def migrate(session):
    # 检测列是否已存在（兼容已有数据库）
    inspector = sa.inspect(session.bind)
    columns = {col["name"] for col in inspector.get_columns("papers")}
    if "paper_type" not in columns:
        if session.bind.dialect.name == "sqlite":
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN paper_type VARCHAR(16)"))
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN type_confidence FLOAT"))
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN classification_source VARCHAR(32)"))
        else:  # postgresql
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN paper_type VARCHAR(16)"))
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN type_confidence FLOAT"))
            session.execute(sa.text("ALTER TABLE papers ADD COLUMN classification_source VARCHAR(32)"))
        session.execute(sa.text("CREATE INDEX ix_papers_paper_type ON papers(paper_type)"))

    # 从 comprehensive_analysis JSON blob backfill
    papers = session.execute(sa.select(Paper)).scalars().all()
    for paper in papers:
        if paper.paper_type or not paper.comprehensive_analysis:
            continue
        ca = paper.comprehensive_analysis
        paper.paper_type = ca.get("paper_type")
        paper.type_confidence = ca.get("type_confidence")
        paper.classification_source = "comprehensive_backfill"
    session.commit()
```

3. Docker 已有数据库兼容：脚本检测列是否存在再 ALTER，不重建。

---

#### 缺口 C2：Docling 图片解析双重 bug + `prov.bbox` 数据丢失

**Codex 诊断**：`docling_parser.py` 第 114-126 行有两个叠加 bug：
1. 字段名是 `captions`（复数），代码写了 `caption`（单数），`item.get("caption")` 永远返回 None
2. 即使取到 `captions`，值是 `$ref` 引用，还需要二次解析
3. `prov` 字段（含 `bbox` 坐标和 `page_no`）被完全丢弃，Level 1 图片截取没有数据基础

**Codex 额外发现**：`_extract_tables()` 第 100-112 行有完全相同的双重 bug（第 106 行 `item.get("caption")` 同样应为 `captions`）。

**源码验证**：✅ 全部确认。第 121 行 `item.get("caption")` 确实应为 `item.get("captions")`；第 106 行同理；`prov` 字段确实未保留。

**修正方案**：

重写 `_extract_figures()` 和 `_extract_tables()`，保留 `prov.bbox`、解析 `$ref` 引用：

```python
@staticmethod
def _resolve_caption_ref(item: dict, payload: dict[str, Any], fallback_index: int, prefix: str = "Figure") -> str:
    """解析 Docling $ref 引用，获取真实 caption 文本"""
    captions = item.get("captions") or []
    for cap_ref in captions:
        ref_path = cap_ref.get("$ref", "")
        if ref_path.startswith("#/texts/"):
            idx = int(ref_path.split("/")[-1])
            texts = payload.get("texts", [])
            if idx < len(texts):
                return texts[idx].get("text", "").strip()
    return item.get("label", "") or f"{prefix} {fallback_index}"

@staticmethod
def _extract_bboxes(item: dict) -> list[dict]:
    """从 prov 中提取 bbox 坐标和页码"""
    bboxes = []
    for prov in item.get("prov") or []:
        bbox = prov.get("bbox")
        if bbox:
            bboxes.append({
                "page_no": prov.get("page_no"),
                "bbox": {
                    "l": bbox.get("l"), "t": bbox.get("t"),
                    "r": bbox.get("r"), "b": bbox.get("b"),
                },
            })
    return bboxes

@staticmethod
def _extract_figures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    figures = payload.get("figures") or payload.get("pictures") or []
    normalized = []
    for index, item in enumerate(figures, start=1):
        normalized.append({
            "caption": DoclingParser._resolve_caption_ref(item, payload, index, "Figure"),
            "page": item.get("page_no") or item.get("page"),
            "figure_role": item.get("role") or "unknown",
            "bboxes": DoclingParser._extract_bboxes(item),  # ← 新增：保留 bbox 数据
        })
    return normalized

@staticmethod
def _extract_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or payload.get("table_items") or []
    normalized = []
    for index, item in enumerate(tables, start=1):
        normalized.append({
            "caption": DoclingParser._resolve_caption_ref(item, payload, index, "Table"),
            "markdown_content": item.get("markdown") or item.get("text") or "",
            "page": item.get("page_no") or item.get("page"),
            "extraction_source": "docling",
            "bboxes": DoclingParser._extract_bboxes(item),  # ← 新增
        })
    return normalized
```

**对 Level 1 的影响**：修正后 `_extract_figures()` 输出包含 `bboxes` 字段，Level 1 的 PyMuPDF 截取可直接使用。原方案 12.2.1 的代码示例不需要修改逻辑，只需引用 `bboxes` 字段即可。

---

#### 缺口 C3：arXiv 搜索不支持 Boolean 查询格式

**Codex 诊断**：`discovery_service.py` 第 147 行 `f"all:{query}"` 直接拼接 Boolean 查询，但 arXiv API 的 `all:` 字段不支持 `AND`/`OR`/`()` 语法，含这些的查询会被当字面字符串。

**源码验证**：✅ 确认。第 147 行确实是 `params={"search_query": f"all:{query}", ...}`。

**影响评估**：A 类论文在方案中分配 60% arXiv 配额，但 A 类最需要 Boolean 精确查询（"DFT AND catalyst AND NOT experimental"）。直接扔进 `all:` 字段，搜索质量反而不如不分类。

**修正方案**：新增 arXiv 查询格式转换器，在 `_search_arxiv()` 调用前将 Boolean 表达式转为 arXiv 原生语法：

```python
@staticmethod
def _convert_to_arxiv_query(boolean_query: str) -> str:
    """将分类感知改写的 Boolean 查询转为 arXiv 原生语法
    
    示例输入: "CO2 electroreduction AND DFT AND (catalyst OR single-atom)"
    示例输出: "all:CO2+AND+all:electroreduction+AND+all:DFT+AND+(all:catalyst+OR+all:single-atom)"
    
    arXiv 语法规则:
    - 字段前缀: ti:(标题) abs:(摘要) all:(所有)
    - 逻辑连接: +AND+ +OR+ +ANDNOT+
    - 括号: ( ) 分组
    - 短语: 用双引号包裹
    """
    tokens = boolean_query.split()
    result_parts = []
    for token in tokens:
        upper = token.upper()
        if upper in ("AND", "OR", "ANDNOT"):
            result_parts.append(f"+{upper}+")
        elif token.startswith("(") or token.endswith(")"):
            result_parts.append(token)  # 保留括号
        elif token.startswith('"') or token.endswith('"'):
            result_parts.append(f'all:{token}')  # 短语搜索
        else:
            result_parts.append(f"all:{token}")
    return "".join(result_parts)
```

在 `_search_arxiv()` 中使用：

```python
def _search_arxiv(self, query: str, limit: int) -> list[dict[str, Any]]:
    # 如果查询含 Boolean 语法，先转换
    if any(kw in query.upper() for kw in (" AND ", " OR ", " NOT ")):
        arxiv_query = self._convert_to_arxiv_query(query)
    else:
        arxiv_query = f"all:{query}"
    # ... 其余不变，用 arxiv_query 替换原来的 f"all:{query}"
```

---

### 13.2 🟠 设计偏差（执行前需对齐）

#### 偏差 D1：OpenAlex `type` 过滤参数写法有误

**Codex 诊断**：方案 3.2.2 写"R 类可直接加 `&type=review`"，但 OpenAlex API 实际语法是 `filter=type:review`。

**源码验证**：✅ 确认。当前 `_search_openalex()` 只用了 `search` 参数（第 104-105 行），未传 `filter`。

**修正**：方案中所有涉及 OpenAlex 类型过滤的描述，统一改为：

```
R 类：params = {"search": query, "filter": "type:review", "per-page": limit}
A 类：params = {"search": query, "filter": "type:article", "per-page": limit}
C 类：params = {"search": query, "filter": "type:article", "per-page": limit}（不加额外过滤，靠关键词区分）
```

实现时在 `_search_openalex()` 中新增 `type_filter` 参数：

```python
def _search_openalex(self, query: str, limit: int, type_filter: str | None = None) -> list[dict[str, Any]]:
    params = {"search": query, "per-page": per_page}
    if type_filter:
        params["filter"] = type_filter
    # ...
```

---

#### 偏差 D2：findpapers Engine 的 `[query]` 包裹与 Boolean 查询冲突

**Codex 诊断**：`_search_via_engine()` 第 197 行 `wrapped_query = query if "[" in query else f"[{query}]"` 会把 Boolean 查询错误包裹。

**源码验证**：✅ 确认。Boolean 查询含 `()` 括号，但 findpapers 要的是 `[]` 括号，两者语法完全不同。

**修正**：分类感知改写后的 Boolean 查询**不应传入 `_search_via_engine()`**。搜索源需要分流处理：

```python
# 在 DiscoveryService.search() 中
for provider in active_providers:
    if provider == "openalex":
        items = self._search_openalex(normalized_query, ...)
    elif provider == "arxiv":
        items = self._search_arxiv(normalized_query, ...)
    elif provider in ("pubmed", "semantic_scholar", "x_mol"):
        # 走 findpapers 的源：传原始关键词，不传 Boolean 表达式
        simple_query = self._strip_boolean_syntax(normalized_query)  # 去掉 AND/OR/NOT
        items = self._search_via_engine(simple_query, [provider], per_provider_limit)
```

新增 `_strip_boolean_syntax()` 方法，将 Boolean 查询还原为简单关键词串，再交给 findpapers。

---

#### 偏差 D3：WritingCard 在 Comprehensive 之前运行，派生关系不可实现

**Codex 诊断**：`extraction_pipeline.py` 第 53-64 行的执行顺序是 `dft_settings → catalyst → dft_results → electrochemical → mechanism → writing_card → comprehensive`。WritingCard（第 59 行）在 Comprehensive（第 60 行）之前跑，但方案 Stage 4.3 说 WritingCard 的 `paper_type` 要从 Comprehensive 派生。

**源码验证**：✅ 确认。当前顺序下 WritingCard 无法读取分类结果。

**修正**：两阶段重构后的正确顺序：

```
Stage 2a: comprehensive（仅跑分类字段）→ 得到 paper_type
Stage 2b: 按 paper_type 激活抽取器 → writing_card 最后跑，从 comprehensive 派生 paper_type
```

具体实现约束：
- `ComprehensiveExtractor` 在 Stage 2a **只跑分类部分**（`paper_type` + `type_confidence` + `classification_source`），不跑完整分析（省 ~80% token）
- Stage 2b 的抽取器组合按 `paper_type` 决定，WritingCard **放在最后一个**运行
- WritingCard 的 `paper_type` 从 Stage 2a 的分类结果派生（A→computational, B→mixed, C→experimental, R→review）

---

#### 偏差 D4：`metadata_only` 论文的 Stage 2a 路径未设计

**Codex 诊断**：当 PDF 下载失败时系统保存仅含元数据的论文。方案 3.3.1 说快速分类输入是"abstract + 首段 markdown + 标题+作者+期刊"，但 metadata_only 论文没有 markdown，可能连 abstract 都没有。

**源码验证**：✅ 合理。这是方案未覆盖的边界情况。

**修正**：metadata_only 论文走降级分类路径：

```python
def classify_paper(paper: Paper, document: UnifiedPaperDocument | None) -> str:
    if document is None:
        # metadata_only 降级：仅靠标题+期刊做规则匹配
        return _rule_based_classify(paper.title, paper.journal)
    
    if not document.sections and not document.abstract:
        # 有 PDF 但无有效文本（扫描件等）
        return _rule_based_classify(paper.title, paper.journal)
    
    # 正常路径：LLM 快速分类
    return _llm_classify(document)

def _rule_based_classify(title: str | None, journal: str | None) -> str:
    """基于标题关键词+期刊名的规则分类"""
    text = f"{title or ''} {journal or ''}".lower()
    computational_kw = ["dft", "density functional", "ab initio", "first-principles", "molecular dynamics"]
    experimental_kw = ["synthesis", "catalyst preparation", "in-situ", "operando"]
    
    has_comp = any(kw in text for kw in computational_kw)
    has_exp = any(kw in text for kw in experimental_kw)
    
    if has_comp and has_exp:
        return "B"  # 计算+实验
    elif has_comp:
        return "A"  # 纯计算
    elif has_exp:
        return "C"  # 纯实验
    return "Unknown"
```

- `classification_source = "rule_heuristic"`（与 LLM 分类来源的 `"comprehensive_llm"` 区分）
- metadata_only 论文**不做 Stage 2b 差异化抽取**（反正没有全文可抽）

---

### 13.3 🟡 实现时需注意的问题

#### 遗漏 E1：`_evidence_score` 打分规则混合了计算类和实验类关键词

**Codex 诊断**：`extraction_pipeline.py` 第 232 行的正则 `(adsorption|barrier|free energy|bader|charge|dos|capacity|cycle|xps|xrd|exafs|xanes)` 同时包含计算类和实验类关键词，差异化抽取后这个混合打分可能产生置信度偏置。

**修正**：按 `paper_type` 分类调整打分关键词：

```python
COMPUTATIONAL_KEYWORDS = r"(adsorption|barrier|free energy|bader|charge|dos|d.band)"
EXPERIMENTAL_KEYWORDS = r"(capacity|cycle|xps|xrd|exafs|xanes|sem|tem|eis|cv|lsv)"

if paper_type and paper_type.startswith("A"):
    keyword_pattern = COMPUTATIONAL_KEYWORDS
elif paper_type and paper_type.startswith("C"):
    keyword_pattern = EXPERIMENTAL_KEYWORDS
else:  # B / R / Unknown
    keyword_pattern = r"(adsorption|barrier|free energy|bader|charge|dos|capacity|cycle|xps|xrd|exafs|xanes)"
```

---

#### 遗漏 E2：搜索结果预分类的代码插入点未指定

**Codex 诊断**：方案 3.2.3 提出在下载前做轻量预分类，决定是否跳过下载，但未指定插入层。

**修正**：插入点在 `papers.py` 的 AI workflow 端点，在 `DiscoveryService.search()` 返回结果后、调用 `paper_ingestion` 之前：

```
search_results = discovery_service.search(...)
    ↓
pre_filter(search_results, target_types)  ← 新增：按 target_types 过滤不相关结果
    ↓
for result in filtered_results:
    paper_ingestion.ingest(...)
```

不在 `DiscoveryService` 内部实现，因为 `DiscoveryService` 是通用搜索服务，不应感知业务语义。

---

#### 遗漏 E3：批量分类后台任务的并发控制未设计

**Codex 诊断**：`/api/papers/classify-batch` 对所有未分类论文跑 Stage 2a（LLM 调用），数百篇论文会瞬间耗尽 LLM API 的 RPM/TPM 配额。

**修正**：

1. 复用现有 `AI_WORKFLOW_JOBS` 机制，批量分类注册为后台任务
2. 速率限制：每批最多 20 篇，每批之间间隔 5 秒（可配置）
3. 进度跟踪：复用 `/api/ai-workflow/status/{job_id}` 轮询
4. 失败重试：单篇失败不阻塞批次，记录到 `failed_items` 列表，批次结束后汇报

```python
# 伪代码
async def classify_batch(library_name: str, batch_size: int = 20, interval: float = 5.0):
    unclassified = session.query(Paper).filter(Paper.paper_type.is_(None)).all()
    job_id = register_job("classify_batch", total=len(unclassified))
    
    failed = []
    for i in range(0, len(unclassified), batch_size):
        batch = unclassified[i:i + batch_size]
        for paper in batch:
            try:
                classify_paper(paper)
                update_job_progress(job_id, completed=i + batch.index(paper) + 1)
            except Exception as e:
                failed.append({"paper_id": str(paper.id), "error": str(e)})
        if i + batch_size < len(unclassified):
            await asyncio.sleep(interval)  # 速率限制
    
    return {"total": len(unclassified), "failed": failed}
```

---

#### 遗漏 E4：`type_confidence = None` 时加权公式产生非预期惩罚

**Codex 诊断**：方案 3.5.3 的公式 `confidence_boost = 1.0 + (paper.type_confidence - 0.5) * 0.4`，当 `type_confidence = 0.0` 时 `boost = 0.8`，反而降低了权重。Unknown 类型应使用中性权重 1.0。

**修正**：加 null guard：

```python
def compute_confidence_boost(paper: Paper) -> float:
    if paper.type_confidence is None or paper.paper_type == "Unknown":
        return 1.0  # 中性权重
    return 1.0 + (paper.type_confidence - 0.5) * 0.4
```

---

#### 遗漏 E5：PyMuPDF 不在 requirements.txt 中

**Codex 诊断**：Level 1 图片提取依赖 `PyMuPDF`（`import fitz`），但 `requirements.txt` 未包含。

**源码验证**：✅ 确认。`requirements.txt` 只有 `pypdf`（第 19 行），没有 `PyMuPDF`。

**修正**：在 `requirements.txt` 中新增：

```
PyMuPDF==1.25.3
```

注意：`PyMuPDF` 和 `pypdf` 不冲突，`pypdf` 用于文本提取（已有），`PyMuPDF` 用于图片截取（新增）。

---

#### 遗漏 E6：PyMuPDF 坐标系与 Docling 坐标系方向相反

**Codex 诊断**：Docling 的 `prov.bbox` 使用 PDF 坐标系（原点在左下角，Y 轴向上），PyMuPDF 默认使用屏幕坐标系（原点在左上角，Y 轴向下）。方案 12.2.1 的代码示例未做坐标转换，会导致截取的图片垂直方向错位。

**修正**：方案 12.2.1 的代码示例修正为：

```python
import fitz  # PyMuPDF

def extract_figure_from_pdf(pdf_path: str, page_no: int, bbox: dict) -> bytes:
    doc = fitz.open(pdf_path)
    page = doc[page_no - 1]  # 0-indexed
    
    # Docling bbox 使用 PDF 坐标系（Y 向上），PyMuPDF 使用屏幕坐标系（Y 向下）
    # 需要转换 Y 轴：pdf_y → screen_y = page_height - pdf_y
    page_height = page.rect.height
    rect = fitz.Rect(
        bbox["l"],
        page_height - bbox["b"],  # 注意：top 和 bottom 要互换
        bbox["r"],
        page_height - bbox["t"],
    )
    
    # 添加 5% padding 防止裁剪偏移
    padding = min(rect.width, rect.height) * 0.05
    rect = fitz.Rect(
        max(0, rect.x0 - padding),
        max(0, rect.y0 - padding),
        min(page.rect.width, rect.x1 + padding),
        min(page.rect.height, rect.y1 + padding),
    )
    
    pix = page.get_pixmap(clip=rect, dpi=200)
    return pix.tobytes("png")
```

---

#### 遗漏 E7：VLM 图片输入需要 base64 编码，不能简单复用 LLMService

**Codex 诊断**：方案 12.3.2 说"复用现有 `LLMService` 通道"，但当前 `LLMService` 是纯文本的 `chat/completions` 调用，没有图片输入的 base64 编码或 multipart 处理。

**修正**：新建 `VLMService` 继承 `LLMService`，增加图片输入路径：

> ⚠️ **v1.3a 修正**（Gemini 第二轮审核）：原伪代码使用 `async def` + `await self._call_api()`，但实际 `LLMService` 是**完全同步**的（使用同步 `OpenAI` 客户端，无 `_call_api` 方法），整个 extraction pipeline 也是同步的。照搬原伪代码会运行时 `AttributeError` 崩溃。已修正为同步版本。

```python
class VLMService(LLMService):
    """视觉语言模型服务，支持图片输入（同步，与 LLMService 架构一致）"""
    
    def analyze_image(self, image_path: str, prompt: str, model: str | None = None) -> dict:
        """发送图片+prompt到视觉模型（同步调用）"""
        import base64
        from pathlib import Path
        
        image_bytes = Path(image_path).read_bytes()
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        
        # 判断图片 MIME 类型
        suffix = Path(image_path).suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        mime_type = mime_map.get(suffix, "image/png")
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64_image}",
                            "detail": "low",  # 论文图片不需要高分辨率
                        },
                    },
                ],
            }
        ]
        
        # 同步调用 OpenAI client（与 LLMService.structured_extract 一致）
        response = self.client.chat.completions.create(
            model=model or self.settings.vlm_default_model or "gpt-4o-mini",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            timeout=self.settings.writer_timeout_seconds or 60.0,
        )
        content = response.choices[0].message.content
        return json.loads(content) if content else {}
```

---

### 13.4 📋 修正后的实施路线图

基于审查结论，调整原方案的 Phase 顺序和内容：

| 执行顺序 | 任务 | 性质 | 工作量 | 前置依赖 |
|---|---|---|---|---|
| **前置 0a** | 修复 Docling 解析器（caption 字段名 + $ref 解析 + 保留 prov.bbox + 表格同修） | 修 bug | 0.5 天 | 无 |
| **前置 0b** | Paper 模型加 paper_type/type_confidence/classification_source + backfill 脚本 | 新字段 | 0.5 天 | 无 |
| **前置 0c** | requirements.txt 新增 PyMuPDF | 依赖 | 0 天 | 无 |
| **Phase 1** | 两阶段抽取（Comprehensive 先跑分类 → 按分类激活差异化抽取）+ 前端分类标签 | 原方案 Phase 1 | 2-3 天 | 前置 0a/0b |
| **Phase 2** | arXiv 查询转换器 + OpenAlex filter 参数 + findpapers 查询分流 + 分类感知搜索 | 原方案 Phase 2 + C3/D1/D2 | 2-3 天 | Phase 1 |
| **Phase 3** | RAG 分类过滤 + Writer 分类模板 + WritingCard 派生统一 + type_confidence null guard | 原方案 Phase 3 + D3/E4 | 2-3 天 | Phase 1 |
| **Phase 4a** | 图片 Level 1（提取+存储+前端展示+API） | 图片基础 | 2 天 | 前置 0a（bbox 数据） |
| **Phase 4b** | VLMService 新建 + 图片 Level 2（VLM 分类 + 前端筛选） | 图片增强 | 1.5 天 | Phase 4a |
| **Phase 5** | 批量分类后台任务（速率限制+进度跟踪）+ metadata_only 降级路径 + _evidence_score 分类适配 | 补全 | 1 天 | Phase 1 |
| **Phase 6** | 图片 Level 3（VLM 数值提取 + FigureDataPoint + 进入 RAG） | 图片深度 | 3 天 | Phase 4b + Phase 3 |

**关键变化**：
- 原方案 Phase 1 之前新增"前置 0"步骤，先修 bug + 加字段 + 加依赖
- Phase 2 增加了 arXiv 查询转换器（C3）和搜索源分流（D1/D2）
- Phase 3 增加了 WritingCard 执行顺序修正（D3）和 null guard（E4）
- 图片功能后移到 Phase 4，因为需要先修 Docling 解析器

---

### 13.5 审查结论验证清单

| Codex 审查结论 | 源码验证 | 采纳 | 修正位置 |
|---|---|---|---|
| 🔴 C1: Paper 模型缺 3 个字段 | ✅ `models.py` 确认 | ✅ | 13.1 缺口 C1 |
| 🔴 C2: Docling 解析器双重 bug + bbox 丢失 | ✅ `docling_parser.py` 确认 | ✅ | 13.1 缺口 C2 |
| 🔴 C3: arXiv 不支持 Boolean 查询 | ✅ `discovery_service.py` 确认 | ✅ | 13.1 缺口 C3 |
| 🟠 D1: OpenAlex type 过滤参数写法 | ✅ 当前只用 search 参数 | ✅ | 13.2 偏差 D1 |
| 🟠 D2: findpapers [query] 包裹与 Boolean 冲突 | ✅ 第 197 行确认 | ✅ | 13.2 偏差 D2 |
| 🟠 D3: WritingCard 在 Comprehensive 之前 | ✅ 第 53-64 行确认 | ✅ | 13.2 偏差 D3 |
| 🟠 D4: metadata_only 论文路径未设计 | ✅ 合理边界情况 | ✅ | 13.2 偏差 D4 |
| 🟡 E1: _evidence_score 混合关键词 | ✅ 第 232 行确认 | ✅ | 13.3 遗漏 E1 |
| 🟡 E2: 预分类插入点未指定 | ✅ 设计遗漏 | ✅ | 13.3 遗漏 E2 |
| 🟡 E3: 批量分类并发控制 | ✅ 设计遗漏 | ✅ | 13.3 遗漏 E3 |
| 🟡 E4: type_confidence=None 惩罚 | ✅ 逻辑错误 | ✅ | 13.3 遗漏 E4 |
| 🟡 E5: PyMuPDF 不在 requirements.txt | ✅ 确认缺失 | ✅ | 13.3 遗漏 E5 |
| 🟡 E6: 坐标系方向相反 | ✅ 技术细节 | ✅ | 13.3 遗漏 E6 |
| 🟡 E7: VLM 不能简单复用 LLMService | ✅ 架构遗漏 | ✅ | 13.3 遗漏 E7 |

---

> **文档版本**：v1.3 | **更新日期**：2026-05-23 | **适用范围**：literature-ai 子系统 + 桌面端
>
> v1.3 更新：整合 Gemini 交叉审核反馈（第十四章），补齐 5 个方案未覆盖的风险点（回滚方案、费用评估、异常容错、跨端同步、测试覆盖），修正实施路线图

## 十四、Gemini 交叉审核反馈与补充

> Gemini 对 v1.2 方案的逐条审核，确认了方案整体思路完整且可落地，同时指出 5 个方案未覆盖的风险点。
> 以下逐条记录反馈、与 v1.2 的对照结论，以及对应的补充设计。

---

### 14.1 已覆盖点确认

Gemini 审核的以下要点在 v1.2 中已完整覆盖，无需额外修改：

| # | Gemini 反馈点 | v1.2 对应位置 | 状态 |
|---|---|---|---|
| 1a | Paper 模型新增 paper_type/type_confidence/classification_source 三个字段并迁移 | §13.1 C1 + §5.1 | ✅ 已覆盖 |
| 1b | Stage 0 意图识别（LLM 快速识别）+ API/模型层结构 | §3.0 + §6.2 | ✅ 已覆盖 |
| 1c | 抽取管线拆分为快速分类 + 差异化抽取 | §3.3 + §3.4 + §13.2 D3 | ✅ 已覆盖 |
| 1d | DiscoveryService 按分类搜索源优先级、arXiv Boolean 转换、OpenAlex filter、findpapers 适配 | §3.2 + §13.1 C3 + §13.2 D1/D2 | ✅ 已覆盖 |
| 1e | RAG paper_type_filter + EVIDENCE_BIAS 证据加权 | §3.5.1 + §3.5.2 | ✅ 已覆盖 |
| 1f | Writer target_paper_type 模板切换 | §3.5.4 | ✅ 已覆盖 |
| 1g | 前端分类徽章、筛选下拉、意图选择器 | §4.1 + §4.3 | ✅ 已覆盖 |
| 2a | OpenAlex 应使用 `filter=type:review` | §13.2 D1（已修正） | ✅ 已修正 |
| 2b | findpapers `[query]` 包裹破坏 Boolean | §13.2 D2（已修正） | ✅ 已修正 |
| 2c | WritingCard paper_type 从 Comprehensive 派生统一 | §3.4.3 + §13.2 D3（已修正） | ✅ 已修正 |
| 2d | Docling 保留 prov.bbox + 解析 $ref caption | §13.1 C2（已修正） | ✅ 已修正 |
| 2e | PyMuPDF 坐标系 Y 轴翻转 | §13.3 E6（已修正） | ✅ 已修正 |
| 2f | type_confidence 加权公式空值 guard | §13.3 E4（已修正） | ✅ 已修正 |
| 2g | 批量分类速率限制 + 进度追踪 + 失败重试 | §13.3 E3（已修正） | ✅ 已修正 |

---

### 14.2 🔴 未覆盖风险点 1：迁移脚本缺少回滚方案

**Gemini 反馈**：当前 `patch_add_paper_type.py`（§13.1 C1）只有正向迁移，没有回滚方案。一旦迁移出错（如列名冲突、数据损坏），无法恢复到迁移前状态。

**v1.2 现状**：§13.1 C1 的迁移脚本包含列存在性检测（兼容已有数据库），但确实没有 `downgrade` 路径和备份策略。

**补充设计**：

#### 14.2.1 迁移前自动备份

```python
# patch_add_paper_type.py 新增
import shutil
from datetime import datetime

def backup_database(db_url: str) -> str:
    """迁移前自动备份数据库文件（仅 SQLite）"""
    if "sqlite" not in db_url.lower():
        return ""  # PostgreSQL 需要由 DBA 手动备份
    
    # 从 db_url 提取文件路径
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if not Path(db_path).exists():
        return ""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_{timestamp}"
    shutil.copy2(db_path, backup_path)
    
    # 验证备份完整性
    backup_size = Path(backup_path).stat().st_size
    original_size = Path(db_path).stat().st_size
    if backup_size < original_size * 0.95:  # 允许 5% 浮动
        raise RuntimeError(f"Backup verification failed: {backup_path} appears incomplete")
    
    return backup_path
```

#### 14.2.2 downgrade.sql 回滚脚本

```sql
-- downgrade_paper_type.sql
-- 从 papers 表中移除 paper_type/type_confidence/classification_source 列
-- 注意：SQLite 不支持 DROP COLUMN（3.35.0 之前），需要重建表

-- SQLite 方案（3.35.0+ 支持 DROP COLUMN）
ALTER TABLE papers DROP COLUMN paper_type;
ALTER TABLE papers DROP COLUMN type_confidence;
ALTER TABLE papers DROP COLUMN classification_source;
DROP INDEX IF EXISTS ix_papers_paper_type;

-- 如果 SQLite 版本 < 3.35.0，需要用传统方式重建表：
-- 1. CREATE TABLE papers_backup AS SELECT [原有列] FROM papers;
-- 2. DROP TABLE papers;
-- 3. ALTER TABLE papers_backup RENAME TO papers;
-- 4. 重建索引
```

#### 14.2.3 迁移执行流程

```
1. 检测列是否已存在 → 已存在则跳过
2. 自动备份 → 记录备份路径
3. 执行 ALTER TABLE + CREATE INDEX
4. 验证：SELECT COUNT(*) FROM papers WHERE paper_type IS NOT NULL
5. 成功 → 输出日志，保留备份 7 天
6. 失败 → 从备份恢复，输出错误详情
```

**决策记录**：每次迁移前自动备份 + 提供 downgrade 脚本 + 迁移后验证。这与 §13.1 C1 的方案合并，执行时遵循此流程。

---

### 14.3 🔴 未覆盖风险点 2：LLM 调用费用与配额未评估

**Gemini 反馈**：批量分类阶段（Phase 1 的 Stage 2a + Phase 5 的批量分类）涉及大量 LLM 调用，但方案未评估费用和配额。

**v1.2 现状**：§3.4.2 有 LLM 调用节省估算，但确实没有费用评估。

**补充设计**：

#### 14.3.1 各阶段 LLM 调用费用估算

| 阶段 | 调用场景 | 单次 token（输入+输出） | 单次费用（DeepSeek） | 单次费用（GPT-4o-mini） |
|---|---|---|---|---|
| Stage 0 意图识别 | 每次搜索 1 次 | ~800 + ~200 = 1K | ¥0.001 | ¥0.003 |
| Stage 2a 快速分类 | 每篇论文 1 次 | ~1,500 + ~300 = 1.8K | ¥0.002 | ¥0.005 |
| Stage 2b 差异化抽取 | 每篇论文 2-7 次 | ~3,000 + ~800 = 3.8K/次 | ¥0.004/次 | ¥0.011/次 |
| VLM 图片分类（Level 2） | 每张图 1 次 | ~1,000 + ~200 = 1.2K | N/A | ¥0.015 |
| VLM 数值提取（Level 3） | 每张图 1 次 | ~1,500 + ~500 = 2K | N/A | ¥0.025 |

**100 篇论文场景费用预估**：

| 操作 | DeepSeek | GPT-4o-mini |
|---|---|---|
| 批量快速分类（100×1 次） | ¥0.2 | ¥0.5 |
| 差异化抽取（100×平均 4.5 次） | ¥1.8 | ¥5.0 |
| VLM 图片分类（100×7 张） | N/A | ¥10.5 |
| **合计（不含 VLM）** | **¥2.0** | **¥5.5** |
| **合计（含 VLM L2）** | **¥2.0** | **¥16.0** |

#### 14.3.2 配额与限流策略

| 维度 | 限制 | 策略 |
|---|---|---|
| **RPM（每分钟请求数）** | DeepSeek: 60 / GPT-4o-mini: 500 | 批量分类每批间隔 5 秒（§13.3 E3 已设计） |
| **TPM（每分钟 token 数）** | DeepSeek: 60K / GPT-4o-mini: 200K | 单次抽取控制在 5K token 以内 |
| **每日总费用** | 用户可配置上限 | `settings.max_daily_llm_cost`（默认 ¥10/天） |
| **单任务费用上限** | 单次批量任务不超过 ¥5 | 超过则暂停，等待用户确认后继续 |
| **费用监控** | 每次 LLM 调用记录 token 数 | `settings.daily_llm_tokens` 累加，每日零点重置 |

#### 14.3.3 配置项

```python
# settings.py 新增
llm_daily_cost_limit: float = 10.0       # 每日 LLM 费用上限（元）
llm_task_cost_limit: float = 5.0         # 单任务 LLM 费用上限（元）
llm_batch_size: int = 20                 # 批量分类每批大小
llm_batch_interval: float = 5.0          # 批次间隔（秒）
llm_default_model: str = "deepseek-chat" # 默认 LLM 模型（费用最优）
vlm_default_model: str = "gpt-4o-mini"  # 默认 VLM 模型
```

---

### 14.4 🟠 未覆盖风险点 3：多源搜索的异常容错

**Gemini 反馈**：方案 §3.2 设计了按分类调 provider 优先级，但未明确单个 provider 失败时的降级策略。

**v1.2 现状**：`discovery_service.py` 当前是并发搜索，单个 provider 失败时该源返回空列表，其他源不受影响。但没有显式的"降级"逻辑——例如 arXiv 超时时，A 类论文应从 OpenAlex 补回。

**补充设计**：

#### 14.4.1 Provider 容错与降级矩阵

| 主力源失败 | 分类 | 降级源 | 降级配额调整 |
|---|---|---|---|
| arXiv 超时/报错 | A1-A4 | OpenAlex 接收原 arXiv 配额 | OpenAlex: 40% → 100% |
| OpenAlex 报错 | B1-B3 | Semantic Scholar 接收原 OpenAlex 配额 | SS: 20% → 80% |
| OpenAlex 报错 | R | Semantic Scholar + Crossref | SS: 20% → 50%, Crossref: 30% |
| PubMed 超时 | C1-C3 | Semantic Scholar 接收原 PubMed 配额 | SS: 20% → 50% |
| 所有源失败 | 任意 | 返回已缓存结果 + 错误提示 | - |

#### 14.4.2 实现：在 DiscoveryService.search() 中增加容错逻辑

```python
async def search(self, query: str, target_types: list[str] | None = None,
                 providers: list[str] | None = None, limit: int = 100) -> list[dict]:
    """分类感知搜索，含 provider 降级"""
    # 1. 根据分类确定 provider 优先级和配额
    provider_quota = self._compute_provider_quota(target_types)
    
    # 2. 并发搜索，收集结果和失败信息
    results_by_provider = {}
    failed_providers = []
    
    for provider, quota in provider_quota.items():
        try:
            items = await self._search_provider(provider, query, quota)
            results_by_provider[provider] = items
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            logger.warning(f"Provider {provider} failed: {e}")
            failed_providers.append(provider)
            results_by_provider[provider] = []
    
    # 3. 降级：将失败源的配额分配给降级源
    if failed_providers:
        fallback_plan = self._compute_fallback(failed_providers, target_types)
        for fallback_provider, extra_quota in fallback_plan.items():
            try:
                extra_items = await self._search_provider(fallback_provider, query, extra_quota)
                results_by_provider[fallback_provider].extend(extra_items)
            except Exception:
                pass  # 降级源也失败，放弃
    
    # 4. 合并去重
    all_results = [item for items in results_by_provider.values() for item in items]
    return self._dedupe(all_results)[:limit]
```

#### 14.4.3 降级配置

```python
# 按分类的 provider 降级映射
FALLBACK_MAP = {
    "A": {"arxiv": ["openalex"], "openalex": ["semantic_scholar"]},
    "B": {"openalex": ["semantic_scholar", "crossref"], "arxiv": ["openalex"]},
    "C": {"openalex": ["semantic_scholar"], "pubmed": ["semantic_scholar", "crossref"]},
    "R": {"openalex": ["semantic_scholar", "crossref"]},
}
```

---

### 14.5 🟠 未覆盖风险点 4：跨端（Web 与桌面）分类同步机制

**Gemini 反馈**：Web 端（literature-ai）和桌面端（PySide6）的分类数据可能不同步——例如 Web 端做了批量分类，桌面端刷新后看到的还是未分类。

**v1.2 现状**：§5.1 的 `paper_type`/`type_confidence`/`classification_source` 存在 SQLite 数据库中。桌面端通过 `LiteratureAIClient`（HTTP 客户端）与 Web 端通信，但确实没有显式的同步机制说明。

**补充设计**：

#### 14.5.1 当前架构下的同步机制

当前架构是**单一数据源**——桌面端通过 HTTP API 访问 Web 端的 SQLite 数据库，不存在双写场景：

```
桌面端 → HTTP API → Web 端 FastAPI → SQLite（唯一数据源）
```

因此，**分类数据天然是一致的**——桌面端每次查看都是从同一个数据库读取。只要 Web 端更新了 `paper_type`，桌面端刷新后即可看到。

#### 14.5.2 需要注意的边缘情况

| 场景 | 问题 | 解决方案 |
|---|---|---|
| 桌面端缓存了旧的论文列表 | 用户在 Web 端做了批量分类，桌面端列表未刷新 | 列表页添加"刷新"按钮；切换库时自动重新拉取 |
| 桌面端离线操作 | 用户在桌面端添加了论文但未同步到 Web | 桌面端的"添加论文"功能走 Web API，不存在离线添加 |
| 分类字段在 API 响应中缺失 | 桌面端渲染时 paper_type 为 null | 前端渲染兜底：null → 显示"未分类"灰色标签 |
| 批量分类进行中，桌面端查询到中间状态 | 部分论文已分类、部分未分类 | `classification_source = "quick"` 标记为"自动分类（待确认）"，与 `"full"` 区分 |

#### 14.5.3 API 响应保证

所有返回论文数据的 API 端点，必须在响应中包含 `paper_type`/`type_confidence`/`classification_source` 三个字段：

```python
# schemas/api.py — PaperResponse 新增字段
class PaperResponse(BaseModel):
    # ... 原有字段 ...
    paper_type: str | None = None
    type_confidence: float | None = None
    classification_source: str | None = None
```

桌面端的 `LiteratureAIClient` 解析响应时自然获得这些字段，无需额外同步逻辑。

**结论**：当前架构是单数据源 + HTTP API，**不需要显式的跨端同步机制**。只需确保 API 响应包含分类字段，桌面端渲染有兜底即可。

---

### 14.6 🟠 未覆盖风险点 5：测试覆盖不足

**Gemini 反馈**：建议为意图识别、查询改写、抽取矩阵等关键路径补充单元/集成测试。

**v1.2 现状**：§9 风险与缓解中提到"LLM 分类不准"的风险，但确实没有测试策略。

**补充设计**：

#### 14.6.1 各阶段测试矩阵

| 阶段 | 测试类型 | 测试重点 | 优先级 |
|---|---|---|---|
| **Stage 0 意图识别** | 单元测试 | 固定输入 → 期望 target_types；降级到规则匹配 | P0 |
| **Stage 1 查询改写** | 单元测试 | 各分类的领域词表注入是否正确 | P0 |
| **Stage 2 搜索源适配** | 单元测试 | arXiv Boolean 转换器；OpenAlex filter 参数构造；findpapers 简化查询 | P0 |
| **Stage 2 搜索源适配** | 集成测试 | 单个 provider 失败 → 降级源被激活 | P1 |
| **Stage 3 快速分类** | 单元测试 | metadata_only → 规则匹配；正常 → LLM 分类 | P0 |
| **Stage 3 两阶段抽取** | 单元测试 | paper_type=A1 → 激活 3 个抽取器；paper_type=Unknown → 全量 | P0 |
| **Stage 4 差异化抽取矩阵** | 单元测试 | 11 种 paper_type × 7 个抽取器的激活/跳过组合 | P1 |
| **Stage 5 RAG 分类过滤** | 单元测试 | paper_type_filter=["A"] → SQL WHERE 扩展到 A1-A4 | P0 |
| **Stage 5 证据加权** | 单元测试 | EVIDENCE_BIAS 对不同分类的分数影响 | P1 |
| **Stage 5 Writer 模板** | 单元测试 | target_paper_type=R → 使用综述模板（无 Methods） | P1 |
| **迁移脚本** | 单元测试 | 列不存在 → ALTER TABLE；列已存在 → 跳过；backfill 逻辑 | P0 |
| **跨端 API** | 集成测试 | API 响应包含 paper_type 等新字段 | P1 |

#### 14.6.2 测试文件规划

```
literature-ai/backend/tests/
├── test_intent_recognition.py       # Stage 0 意图识别
├── test_query_rewrite.py            # Stage 1 查询改写
├── test_search_provider_fallback.py # Stage 2 搜索源容错（新增）
├── test_arxiv_query_converter.py   # Stage 2 arXiv 查询转换
├── test_quick_classification.py    # Stage 3 快速分类
├── test_extraction_matrix.py        # Stage 4 差异化抽取矩阵
├── test_rag_type_filter.py          # Stage 5 RAG 分类过滤
├── test_writer_type_template.py     # Stage 5 Writer 分类模板
├── test_paper_type_migration.py     # 迁移脚本
└── test_paper_type_api_fields.py    # API 响应字段（新增）
```

#### 14.6.3 测试策略

- **P0 测试**：在对应 Phase 完成时立即编写，作为 Phase 交付条件
- **P1 测试**：在所有 Phase 完成后的 Polish 阶段补充
- **回归测试**：每个 Phase 完成后运行全量测试，确认不影响已有功能
- **LLM mock**：意图识别和查询改写的测试使用固定 mock 返回值，不实际调用 LLM

---

### 14.7 📋 修正后的实施路线图（v1.3 最终版）

基于 Gemini 反馈，在 §13.4 路线图基础上增加以下调整：

| 执行顺序 | 任务 | 性质 | 工作量 | 前置依赖 | 新增/调整 |
|---|---|---|---|---|---|
| **前置 0a** | 修复 Docling 解析器 | 修 bug | 0.5 天 | 无 | - |
| **前置 0b** | Paper 模型加字段 + **backfill 脚本 + 自动备份 + downgrade.sql** | 新字段 | 0.5 天 | 无 | **+备份+回滚** |
| **前置 0c** | requirements.txt 新增 PyMuPDF | 依赖 | 0 天 | 无 | - |
| **Phase 1** | 两阶段抽取 + 前端分类标签 + **P0 单元测试** | 核心功能 | 3 天 | 前置 0a/0b | **+P0测试** |
| **Phase 2** | 搜索适配 + 分类感知搜索 + **provider 降级容错** + **费用监控配置** | 搜索增强 | 3 天 | Phase 1 | **+容错+费用** |
| **Phase 3** | RAG 分类过滤 + Writer 分类模板 + WritingCard 派生 | RAG+写作 | 2-3 天 | Phase 1 | - |
| **Phase 4a** | 图片 Level 1 | 图片基础 | 2 天 | 前置 0a | - |
| **Phase 4b** | VLMService + 图片 Level 2 | 图片增强 | 1.5 天 | Phase 4a | - |
| **Phase 5** | 批量分类 + metadata_only 降级 + _evidence_score 适配 | 补全 | 1 天 | Phase 1 | - |
| **Phase 6** | 图片 Level 3 | 图片深度 | 3 天 | Phase 4b + Phase 3 | - |
| **Polish** | **P1 集成测试** + **回归测试** + 分栏拖拽 + 桌面端适配 | 质量保证 | 1-2 天 | 所有 Phase | **+回归测试** |

**关键变化**（相比 v1.2 §13.4）：
- 前置 0b 增加自动备份 + downgrade 回滚脚本
- Phase 1 增加P0 单元测试作为交付条件
- Phase 2 增加 provider 降级容错 + LLM 费用监控配置
- 新增 Polish 阶段包含 P1 集成测试 + 全量回归测试
- 每个阶段完成后必须运行回归测试

---

### 14.8 风险与缓解（v1.3 增补）

在 §9 原有风险表基础上，新增以下 5 项：

| 风险 | 可能性 | 影响 | 缓解措施 |
|---|---|---|---|
| 迁移失败导致数据库损坏 | 低 | 无法入库/检索 | 迁移前自动备份；提供 downgrade.sql；迁移后验证 |
| LLM 费用超预期 | 中 | 用户费用过高 | 每日/每任务费用上限；默认使用 DeepSeek（最便宜）；VLM 按需调用 |
| 搜索源大面积故障 | 低 | 搜索结果大幅减少 | Provider 降级矩阵（§14.4）；已缓存结果兜底 |
| 批量分类 LLM 配额耗尽 | 中 | 分类任务中断 | 批次间隔 5 秒；配额即将耗尽时暂停，等待用户确认后继续 |
| 分类数据前端展示不一致 | 低 | 用户困惑 | API 响应保证包含分类字段；null 兜底为"未分类"；classification_source 区分确认状态 |

---

### 14.9 Gemini 第二轮审核：代码架构层面修正（v1.3a）

> Gemini 在审核 v1.3 后，进一步深入对照实际代码库（`llm_service.py` 和 `models.py`），发现 2 个代码架构层面的硬伤。
> 以下逐条记录并说明修正方案。

#### 14.9.1 🔴 `VLMService` 同步/异步架构不匹配（阻塞 Phase 4b）

**Gemini 发现**：

§13.3 E7 中 `VLMService` 的伪代码使用了 `async def analyze_image` + `await self._call_api()`。但实际代码中：

- `LLMService`（`backend/app/services/llm_service.py`）使用**同步** `OpenAI` 客户端（`self.client = OpenAI(...)`），调用方式为 `self.client.chat.completions.create(...)`（阻塞同步）
- `LLMService` 中**不存在** `_call_api` 或 `_parse_json_response` 方法
- 整个 extraction pipeline（`extraction_pipeline.py`）也是同步调用的

如果开发时照搬原伪代码，运行时会直接抛出 `AttributeError`（`_call_api` 不存在）或 `TypeError`（同步方法被 await）导致服务崩溃。

**已修正**：§13.3 E7 的伪代码已从 `async def` + `await self._call_api()` 修正为 `def analyze_image()` + `self.client.chat.completions.create()`（同步调用），与 `LLMService.structured_extract` 的调用模式完全一致。

**架构决策**：`VLMService` 保持同步，与现有管线架构一致。如果未来需要并发调用多张图片，应在调用侧（如批量处理循环）使用 `concurrent.futures.ThreadPoolExecutor`，而非将服务本身改为 async。

#### 14.9.2 🔴 `FigureDataPoint` 数据库模型缺失（阻塞 Phase 6）

**Gemini 发现**：

方案在 §12.4.3 定义了 `FigureDataPoint` 的字段表，在 Phase 6 提到"图片 Level 3（VLM 数值提取 + FigureDataPoint + 进入 RAG）"。但第五章"数据库模型变更"只列出了 `papers` 表的 3 个分类字段，**完全遗漏了 `FigureDataPoint` 的表结构定义**。

当前 `models.py` 中的 `PaperFigure` 只有 5 个字段（id/paper_id/caption/image_path/page/figure_role），无法承载 VLM 提取的数值数据。

**已修正**：§5.4 新增了 `FigureDataPoint` 完整表结构（独立新表方案），包括 SQLAlchemy 模型定义、索引设计、迁移说明和备选方案说明。

**关键设计决策**：
- 采用**独立新表**（而非 `PaperFigure` 的 JSON 列），便于结构化 SQL 查询和 RAG 精确检索
- `paper_id` 冗余存储（同时有 `figure_id` 外键），便于按论文直接查询，无需 JOIN
- 迁移脚本独立（`patch_add_figure_data_point.py`），在 Phase 6 实施时创建，不在前置 0b 中
- 同样需要 downgrade 回滚脚本

---

> **文档版本**：v1.3a | **更新日期**：2026-05-23 | **适用范围**：literature-ai 子系统 + 桌面端
>
> v1.3a 更新：整合 Gemini 第二轮审核反馈（§14.9），修正 VLMService 同步/异步架构不匹配问题，补充 FigureDataPoint 数据库模型定义（§5.4）
