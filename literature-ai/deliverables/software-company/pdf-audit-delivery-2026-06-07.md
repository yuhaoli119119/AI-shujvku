# Literature AI — PDF 四层架构审计 & 多AI协同迭代 交付报告

> **日期**: 2026-06-07  
> **版本**: v1.0  
> **验收方**: Gemini  
> **项目**: Literature AI (literature-ai)  

---

## 一、TL;DR

基于 Gemini 提出的 PDF 四层架构框架（Parsing → Structure Restoration → Slicing/Indexing → Tool Calling），完成了全量诊断、4 项 P0 修复、5 项逻辑 Bug 修复、多 AI 协同架构设计（Blackboard 模式），以及审核可见性与分歧检测工具链。最终 MCP 工具数从 20 增至 **25**，建立了"雁过留声"的多 AI 共享记忆机制。

---

## 二、变更总览

| 阶段 | 内容 | 涉及文件 | 状态 |
|------|------|---------|------|
| 诊断报告 | 对照四层架构输出审计报告 | — | ✅ 完成 |
| 优先级校准 | Gemini 校正 P0~P2 排序 | — | ✅ 完成 |
| P0-1 | 移除表格 2000 字符截断 | `paper_ingestion.py` | ✅ 完成 |
| P0-2 | 图注增强为 caption+VLM摘要+数值点 | `paper_ingestion.py` | ✅ 完成 |
| P0-3 | 新增 `read_paper_page` 工具 | `server.py` | ✅ 完成 |
| P0-4 | 新增 `analyze_chart` 工具 | `server.py` | ✅ 完成 |
| Bug 修复 ×5 | 协议追踪/Session生命周期/figure查询等 | 多文件 | ✅ 完成 |
| PostgreSQL 防误注 | 6 个文件添加 NOT SQLite 注释 | 见下文 | ✅ 完成 |
| Blackboard 模式 | `analyze_chart` 自动落盘 PaperNote | `server.py` | ✅ 完成 |
| 审核可见性 | `review_figure` + `get_review_coverage` + `get_field_disputes` | `server.py`, `auth.py` | ✅ 完成 |
| 权限拆分 | `review_dft` 能力 + `require_mcp_capability_any()` | `auth.py`, `server.py` | ✅ 完成 |
| 前端向导 | 提取向导页面 + 原子工具决策树 | `index.html` | ✅ 完成 |

---

## 三、详细变更清单

### 3.1 P0 修复：移除表格 2000 字符截断

**文件**: `backend/app/services/paper_ingestion.py`

**问题**: 原代码将 `table_text[:2000]` 硬截断后传入分块，导致长表格（如 DFT 参数矩阵）数据丢失。

**修复**: 
- 删除 `truncated_text = table_text[:2000] + ...` 逻辑
- 直接将完整 `table_text` 传给 `_add_section_with_chunks()`
- 由 800-token 滑动窗口自动处理分块，不丢失任何数据

**验证**: 搜索代码中已无 `truncat` 关键词（除注释外）。

---

### 3.2 P0 修复：图注增强为多模态向量索引

**文件**: `backend/app/services/paper_ingestion.py`

**问题**: 原图注仅使用 `caption` 文本做向量索引，VLM 生成的视觉摘要和数值点未参与索引，导致"绿色曲线交点"等语义查询无法命中。

**修复**:
```python
enhanced_text = caption_text
if figure.content_summary:
    enhanced_text += f"\n[AI Visual Summary]: {figure.content_summary}"
if figure.numerical_data_points:
    enhanced_text += f"\n[Extracted Data]: {json.dumps(figure.numerical_data_points, ensure_ascii=False)}"
```
- `enhanced_text` 传入 `_add_section_with_chunks()`，VLM 摘要和数值点全部参与向量索引
- 新增 `import json` 到顶层导入

**效果**: 用户查询"CO2 吸附能"时，即使 caption 只写"Figure 3"，也能通过 VLM 摘要命中。

---

### 3.3 新增工具：`read_paper_page`

**文件**: `backend/app/mcp/server.py` (L903-978)

**功能**: 按页码读取论文的完整版面内容，返回该页所有 sections、tables、figures。

**关键设计**:
- **Sections**: 按 `page_start ≤ page ≤ page_end` 范围查询，支持跨页章节
- **Tables**: 精确页码匹配 (`PaperTable.page == page`)
- **Figures**: 精确页码匹配 + 返回 `figure_refs` 包含 `figure_id`，供 `analyze_chart` 接力
- 每个 figure 输出包含: `Figure ID: xxx  ← use analyze_chart with this ID`

**能力要求**: `read_papers`

---

### 3.4 新增工具：`analyze_chart`（三阶段设计 + 自动落盘）

**文件**: `backend/app/mcp/server.py` (L981-1070)

**功能**: 用 VLM 对指定图表进行定向追问，结果自动落盘为共享 PaperNote（Blackboard 模式）。

**三阶段设计**（解决 Session 生命周期问题）:

| 阶段 | 操作 | DB Session |
|------|------|-----------|
| Phase 1 | 读取 figure 元数据 + 关闭 session | 开 → 关 |
| Phase 2 | VLM 推理调用（10-30s） | 无 |
| Phase 3 | 新 session 写入 PaperNote + AuditLog | 开 → 关 |

**自动落盘**:
- 写入 `PaperNote(field_name="figure_analysis")`
- 写入 `AuditLog(action="analyze_chart_auto_note")`
- 返回 `auto_note_created: True`
- **雁过留声**: 其他 AI 和人类可通过 `list_notes` 查看分析痕迹

**能力要求**: `read_papers`

---

### 3.5 新增工具：`review_figure`

**文件**: `backend/app/mcp/server.py` (L1078-1131)

**功能**: 对指定图表记录审核结论，结论自动落盘为共享 PaperNote。

**verdict 选项**:
- `verified` — AI 摘要与图片一致
- `needs_attention` — 摘要不完整或有误导
- `incorrect` — 摘要与图片矛盾

**自动落盘**:
- 写入 `PaperNote(field_name="figure_review")`
- 写入 `AuditLog(action="review_figure")`

**能力要求**: `review_corrections` 或 `review_dft`（使用 `require_mcp_capability_any`）

---

### 3.6 新增工具：`get_review_coverage`

**文件**: `backend/app/mcp/server.py` (L1134-1260)

**功能**: 汇总论文各维度审核覆盖率，定位审核盲区。

**覆盖维度**:

| 维度 | 数据来源 | 状态判定 |
|------|---------|---------|
| Figures | `PaperNote(field_name="figure_review")` + `figure_analysis` | reviewed / analyzed / unreviewed |
| Tables | `PaperCorrection(field_name="table")` | has_correction / unreviewed |
| Sections | `PaperCorrection(field_name in ["section","title","text"])` | has_correction / unreviewed |

**返回结构**:
```json
{
  "figures": { "total": N, "reviewed": X, "analyzed_only": Y, "unreviewed": Z, "details": [...] },
  "tables":  { "total": N, "with_corrections": X, "unreviewed": Y, "details": [...] },
  "sections": { "total": N, "with_corrections": X, "unreviewed": Y, "details": [...] }
}
```

**能力要求**: `read_papers`

---

### 3.7 新增工具：`get_field_disputes`

**文件**: `backend/app/mcp/server.py` (L1263-1353)

**功能**: 检测多个 AI 对同一字段给出不同值的冲突，供终审者裁决。

**检测逻辑**:
1. **修正分歧**: 同一 `target_path` 下有 ≥2 条 pending `PaperCorrection`，且 `proposed_value` 不同
2. **图表审核分歧**: 同一 figure caption 下有 ≥2 条 `figure_review` note，且 verdict 不同

**返回结构**:
```json
{
  "correction_disputes": [{ "target_path": "...", "conflict_count": N, "proposals": [...] }],
  "figure_disputes": [{ "caption": "...", "conflicting_verdicts": [...], "reviews": [...] }],
  "total_disputes": N
}
```

**能力要求**: `read_papers`

---

### 3.8 权限拆分：`review_dft` + `require_mcp_capability_any()`

**文件**: `backend/app/mcp/auth.py`

**新增能力**: `review_dft` — 专门用于 DFT 数据审核，权限范围小于 `review_corrections`。

**新增函数**:
```python
def require_mcp_capability_any(*capabilities: str) -> MCPAuthInfo:
    """Check that the MCP key has at least one of the given capabilities."""
```

**使用场景**:
- `verify_dft_result` → `require_mcp_capability_any("review_corrections", "review_dft")`
- `reject_dft_result` → `require_mcp_capability_any("review_corrections", "review_dft")`
- `review_figure` → `require_mcp_capability_any("review_corrections", "review_dft")`

**6 大能力清单**:
| 能力 | 用途 |
|------|------|
| `read_papers` | 读取论文、查证、翻页 |
| `append_notes` | 追加笔记 |
| `propose_corrections` | 提出修正建议 |
| `request_parse` | 请求解析 |
| `review_corrections` | 审核修正（全域） |
| `review_dft` | 审核DFT数据（子集） |

---

### 3.9 PostgreSQL 防误注

**问题**: AI 曾错误假设项目使用 SQLite，引用 SQLite 写锁问题。

**修复**: 在 7 个文件中添加了 `# NOTE: ... PostgreSQL ... NOT SQLite` 注释块：

| 文件 | 注释位置 |
|------|---------|
| `backend/app/db/session.py` | L11-24, 详细说明了 PostgreSQL 特性差异 |
| `backend/app/db/models.py` | L3-5 |
| `backend/app/config.py` | L16-18 |
| `backend/app/utils/protocol_tracking.py` | L11-13 |
| `backend/app/api/system.py` | L130, `positioning` 字段 |
| `backend/app/mcp/server.py` | L993-996, `analyze_chart` Phase 1 注释 |

**关键差异提示**:
- PostgreSQL 支持并发读写，无 SQLite 式文件锁
- UUID 列是原生 PostgreSQL UUID 类型（非 CHAR(32)）
- JSONB 而非 plain JSON
- pgvector 提供 HNSW 向量索引
- 避免在 VLM 长调用期间持有 session（idle-in-transaction timeout）

---

### 3.10 协议追踪补全

**文件**: `backend/app/utils/protocol_tracking.py`

**问题**: `PROTOCOL_FILES` 仅包含 3 个 YAML，遗漏了 4 个。

**修复**: 从 3 项扩展到 **7 项**:

```python
PROTOCOL_FILES = {
    "dft_results": "prompts/dft_results.yaml",
    "dft_ai_protocol": "prompts/dft_ai_protocol.yaml",
    "gemini_audit_protocol": "prompts/gemini_audit_protocol.yaml",
    "dft_settings": "prompts/dft_settings.yaml",           # NEW
    "mechanism_claims": "prompts/mechanism_claims.yaml",     # NEW
    "paper_writer": "prompts/paper_writer.yaml",             # NEW
    "writing_card": "prompts/writing_card.yaml",             # NEW
}
```

---

### 3.11 前端提取向导页面

**文件**: `frontend/pages/extraction_workflow/index.html`

**变更**:
1. "0 自动入库" → "✓ 需人工确认入库"
2. Step 2 描述更新：提及 VLM summaries、lossless long tables、joint text-figure indexing
3. 所有 7 步 prompt 末尾添加 "完整协议请查看设置页 → 提取协议"
4. 新增 **"按需原子工具决策树"** 面板，6 张工具卡片：

| 卡片 | 工具 | 说明 |
|------|------|------|
| 📖 宏观检索 | `query_papers` / `retrieve_evidence` | 从宏观发现目标论文 |
| 📄 翻页查证 | `read_paper_page` | 精确到页的原文查阅 |
| 🔬 图表追问 | `analyze_chart` | VLM 定向追问，结果自动落盘 |
| ✅ 图表审核 | `review_figure` | 记录审核结论 |
| 📊 审核覆盖率 | `get_review_coverage` | 定位审核盲区 |
| ⚡ 分歧检测 | `get_field_disputes` | 发现 AI 间冲突 |

5. Protocol 显示上限从 6 → 7

---

## 四、架构决策记录

### 4.1 Blackboard 模式（雁过留声）

**决策**: 拒绝硬编码角色路由，采用 PaperNote 驱动的灵活协作。

**理由**（Gemini 提议，用户认同）:
- 硬编码"Coder → Reviewer → Approver"管线太刚性
- 同一 AI 可同时具备多个角色
- 论文分析是探索性工作，不适合固定流水线
- `PaperNote` 作为共享黑板，任何 AI 写入的痕迹对其他 AI 可见

**实现**: 
- `analyze_chart` Phase 3 自动将 VLM 结果落盘为 `PaperNote(field_name="figure_analysis")`
- `review_figure` 自动将审核结论落盘为 `PaperNote(field_name="figure_review")`
- 所有落盘都伴随 `AuditLog`，确保溯源

### 4.2 权限拆分（最小权限原则）

**决策**: 从 `review_corrections` 中拆出 `review_dft`。

**理由**: 原设计 `review_corrections` 权限过宽——仅需审核 DFT 数据的 AI 不应拥有修正全域数据的权限。

### 4.3 Session 生命周期管理

**决策**: `analyze_chart` 三阶段设计，VLM 调用在 session 外执行。

**理由**: VLM 推理耗时 10-30s，在此期间持有 PostgreSQL session 会导致：
- 连接池无法回收
- idle-in-transaction timeout
- 事务膨胀

---

## 五、MCP 工具清单（25 个）

### 读取类 (13)
| # | 工具名 | 能力 |
|---|--------|------|
| 1 | `query_papers` | read_papers |
| 2 | `get_paper` | read_papers |
| 3 | `get_codex_context` | read_papers |
| 4 | `get_codex_item` | read_papers |
| 5 | `get_paper_knowledge` | read_papers |
| 6 | `get_dft_review_queue` | read_papers |
| 7 | `retrieve_evidence` | read_papers |
| 8 | `compare_papers` | read_papers |
| 9 | **`read_paper_page`** 🆕 | read_papers |
| 10 | **`analyze_chart`** 🆕 | read_papers |
| 11 | **`get_review_coverage`** 🆕 | read_papers |
| 12 | **`get_field_disputes`** 🆕 | read_papers |
| 13 | `insert_word_citation` | read_papers |

### 写入类 (6)
| # | 工具名 | 能力 |
|---|--------|------|
| 14 | `append_note` | append_notes |
| 15 | `propose_correction` | propose_corrections |
| 16 | `propose_dft_result_correction` | propose_corrections |
| 17 | `import_analysis` | propose_corrections |
| 18 | **`verify_dft_result`** 🔧 | review_corrections **或** review_dft |
| 19 | **`reject_dft_result`** 🔧 | review_corrections **或** review_dft |

### 审核类 (1)
| # | 工具名 | 能力 |
|---|--------|------|
| 20 | **`review_figure`** 🆕 | review_corrections **或** review_dft |

### 解析类 (3)
| # | 工具名 | 能力 |
|---|--------|------|
| 21 | `scan_local_pdfs` | request_parse |
| 22 | `ingest_pdf_batch` | request_parse |
| 23 | `parse_paper` | request_parse |

### 其他 (2)
| # | 工具名 | 能力 |
|---|--------|------|
| 24 | `get_parse_status` | read_papers |
| 25 | `compare_papers` | read_papers |

> 🆕 = 本次新增, 🔧 = 权限变更

---

## 六、文件变更清单

| 文件路径 | 变更类型 | 关键变更 |
|---------|---------|---------|
| `backend/app/mcp/server.py` | 重大修改 | +4 新工具、figure_refs、三阶段设计、自动落盘 |
| `backend/app/mcp/auth.py` | 新增函数 | `require_mcp_capability_any()` |
| `backend/app/services/paper_ingestion.py` | 修改 | 移除截断、图注增强、`import json` |
| `backend/app/api/system.py` | 修改 | agent-guide 更新、PostgreSQL 注释 |
| `backend/app/utils/protocol_tracking.py` | 修改 | 3→7 协议文件、PostgreSQL 注释 |
| `backend/app/db/session.py` | 修改 | PostgreSQL 防误注释块 |
| `backend/app/db/models.py` | 修改 | PostgreSQL 防误注释块 |
| `backend/app/config.py` | 修改 | PostgreSQL 防误注释块 |
| `frontend/pages/extraction_workflow/index.html` | 修改 | 向导文案、原子工具决策树 |

---

## 七、待验收检查项

### 功能验证
- [ ] `read_paper_page` 返回 sections + tables + figures + figure_refs
- [ ] `analyze_chart` 三阶段执行无 session 泄漏
- [ ] `analyze_chart` VLM 结果自动落盘为 `PaperNote(field_name="figure_analysis")`
- [ ] `review_figure` verdict 枚举验证（仅 accepted: verified/needs_attention/incorrect）
- [ ] `review_figure` 结论自动落盘为 `PaperNote(field_name="figure_review")`
- [ ] `get_review_coverage` 返回 figures/tables/sections 三维度覆盖率
- [ ] `get_field_disputes` 检测到修正分歧和图表审核分歧
- [ ] `require_mcp_capability_any()` 正确拒绝无权限请求
- [ ] 长表格（>2000字符）完整保留不截断
- [ ] 图注向量索引包含 VLM 摘要和数值点

### 安全验证
- [ ] `review_dft` 能力可独立于 `review_corrections` 授予
- [ ] `analyze_chart` 不需要 `append_notes` 能力（系统级写入，bypass）
- [ ] `review_figure` 至少需要 `review_corrections` 或 `review_dft` 之一

### 一致性验证
- [ ] 7 个 YAML 协议文件全部被 `PROTOCOL_FILES` 覆盖
- [ ] 前端向导 7 步与后端协议对齐
- [ ] 所有文件中 PostgreSQL 注释一致
- [ ] `agent-guide` 接口描述与实际工具签名一致

---

## 八、已知限制 & 后续建议

| 编号 | 事项 | 优先级 | 说明 |
|------|------|--------|------|
| L-1 | `get_review_coverage` 基于 caption 匹配 figure，若 caption 重复会误判 | P2 | 可改用 figure_id 精确匹配 |
| L-2 | `analyze_chart` VLM 不可用时直接抛异常，无 graceful fallback | P2 | 可返回已有 content_summary 作为降级 |
| L-3 | `get_field_disputes` 仅检测 pending 状态修正，已 resolved 的不参与 | P1 | 需确认这是否符合预期 |
| L-4 | 前端向导缺少 `read_paper_page` 的交互触发点 | P2 | 可在 Step 2 和 Step 5 增加快捷按钮 |
| L-5 | 未实现 `read_paper_page` 的跨页 range 查询（当前仅单页） | P2 | 可扩展为 `page_start, page_end` 参数 |

---

## 九、审计原则回顾

本次迭代遵循以下原则（由 Gemini 提出，用户确认）：

1. **P0/P1/P2 优先级校准**: 多模态图索引 > 长表格 > 按页查证 > 标题层级/自动OCR
2. **Blackboard 模式**: PaperNote 驱动的灵活协作，而非硬编码角色管线
3. **雁过留声**: 所有 AI 分析结果自动落盘，确保跨 agent 可见
4. **最小权限**: 拆分 `review_dft`，避免权限过宽
5. **PostgreSQL 正确性**: 全局标注，防止 AI 误用 SQLite 语义

---

*报告生成时间: 2026-06-07 10:54 CST*
