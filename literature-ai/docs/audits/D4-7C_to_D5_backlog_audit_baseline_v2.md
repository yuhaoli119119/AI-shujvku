# Literature AI - Backlog Audit & Current Baseline

**Date:** 2026-05-31
**Context:** This document audits the current state of documented but unfinished features for the Literature AI project, classifying their readiness, defining clear boundaries, and establishing a strict execution order to prevent accidental high-risk DB modifications.

---

## 一、已完成功能基线 (Completed Baseline)
- **文献检索与解析基础链路**: PDF parsing (Docling/Grobid), exact locator extraction, hybrid retrieval (BM25 + Vector).
- **前端核心页面架构**: Document repository (`/library`), DFT Database (`/dft`), Writing Assistant (`/writing_assistant`) layouts.
- **数据抽取的 Safety Gate (基础版)**: `is_export_eligible_extraction` and `writing_card_gate` ensure that only safe, verified evidence is surfaced.
- **后端数据库性能与鲁棒性**: P0/P1/P2/P3 batch fixes for N+1 queries, UUID handling, and deterministic SQL filtering.

---

## 二、未完成功能状态清单 (Backlog Audit)

### A. 写作引用工作流后续功能 (Writing & Citation Workflow)

| 功能项 | 状态 | 相关文件/依赖 | 禁止触碰边界 |
| :--- | :--- | :--- | :--- |
| **D4-7C 最终验收与文案收口** | `partial_draft_only` | `frontend/pages/writing_assistant/*`, `backend/app/api/writing.py` | 绝对禁止新增 DB 写操作；只做文案修改与 Guardrail 断言测试。 |
| **D4-8 Zotero/BibTeX/CSL 兼容预览** | `documented_not_started` | 新增只读 API 路由及前端组件 | 仅提供 `draft metadata only`，禁止实现自动插入/导出正式引文。 |
| **D4-9 元数据补全工作流** | `documented_not_started` | `app/api/papers` | 只读展示缺失字段；禁止自动联网抓取补齐，禁止自动提升可信度。 |
| **D4-10 Human Verification Promotion** | `blocked_by_safety_gate` | `app/services/evidence_service.py`, 审计日志 | 从 verified 到 safe_verified 必须有人工显式审阅；禁止批量静默晋升。 |
| **D5 写作扩展 (Manuscript/Revision)** | `documented_not_started` | 新增写作子模块 | 不能把未验证证据自动视为 confirmed 事实。 |
| **C1/C2 引文格式/Marker Placeholder** | `partial_draft_only` | `app/services/writing_citation_insertion_service.py` | 需保持 draft 状态直到 D4-8 落地。 |

### B. DFT / ML 数据闭环后续功能 (DFT / ML Data Pipeline)

| 功能项 | 状态 | 相关文件/依赖 | 禁止触碰边界 |
| :--- | :--- | :--- | :--- |
| **DFT ML-ready Export 基础版** | `backend_only` | `app/api/papers/aggregation.py` (已有路由), 前端缺失入口 | 缺 review/evidence/locator 的记录绝对不能进入导出集。 |
| **DFT 数据质量面板** | `backend_only` | `app/api/papers/aggregation.py` (已有路由), 前端缺失页面 | 不新增自动 verified 行为。 |
| **检索/入库任务中心增强** | `documented_not_started` | 任务队列 API 与前端 UI | 失败重试操作绝不能导致重复入库。 |
| **Zotero Adapter 预留接口** | `documented_not_started` | 未定 | 仅做只读对接或元数据拉取预留。 |
| **C3 VLM Token 消耗统计** | `partial_draft_only` | `app/services/*` | 仅作日志审计完善。 |

---

## 三、建议执行顺序 (Strict Execution Order)

按照“先验收已有能力 -> 增加只读能力 -> 最后受控写能力”的原则，严格遵循以下批次：

1. **批次 1**：Backlog 审计与现状基线 (本报告)
2. **批次 2**：D4-7C Writing Assistant 最终验收与文案收口 (收紧 Guardrail)
3. **批次 3**：D4-8 只读 Citation Metadata Preview (无副作用拓展)
4. **批次 4**：D4-9 Metadata Completion Workflow (只读诊断)
5. **批次 5**：DFT ML-ready Export 基础版 (打通数据导出闭环)
6. **批次 6**：DFT 数据质量面板 (透明化拦截原因)
7. **批次 7**：检索/入库任务中心增强 (提升可观测性)
8. **批次 8**：D4-10 Human Verification Promotion Workflow (⚠️ 单独的高风险数据写入批次)
9. **批次 9**：D5 扩展项 (Manuscript/Revision)
