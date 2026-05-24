# 外部 AI 接入知识库 — 实施计划

## 任务目标

在 literature-ai 项目的 MCP 服务端新增 4 个工具，让外部 AI（DeepSeek API、Cursor、VS Code Copilot、Codex 等）能够直接查询知识库、检索跨论文证据、触发 AI 审阅、导入分析结果。

**用户核心诉求**：不相信软件自己分析的准确度，想让外部 AI 来审核和补充。

## 项目位置

```
D:\Desktop\03_代码与开发\爬虫脚步下载文章\literature-ai\backend\
```

---

## 现有架构（不要改，只需基于它扩展）

### MCP 服务端
- **文件**: `app/mcp/server.py`
- **框架**: `FastMCP` (mcp Python SDK)，`streamable_http` 模式
- **挂载**: `app/main.py` 第58行 `app.mount("/mcp", mcp_http_app)`
- **已有 13 个工具**: `query_papers`, `scan_local_pdfs`, `get_paper`, `list_notes`, `append_note`, `propose_correction`, `get_parse_status`, `get_correction_queue`, `get_correction_detail`, `approve_correction`, `reject_correction`, `parse_paper`, `ingest_pdf_batch`
- **认证**: Bearer Token，密钥格式 `source_prefix|display_name|api_key|capability1,capability2`，配置在 `LITAI_MCP_API_KEYS` 环境变量
- **权限集合**: `read_papers`, `request_parse`, `append_notes`, `propose_corrections`, `review_corrections`
- **每个工具开头都调用 `require_mcp_capability("xxx")`** 做权限检查

### RAG Retriever
- **文件**: `app/rag/retriever.py`
- **类**: `Retriever(session, embedding_dimension=64)`
- **核心方法**: `retrieve(query, paper_ids, limit_per_type, target_paper_type, paper_type_filter)` → 返回六类证据 dict:
  - `sections`, `dft_results`, `electrochemical_performance`, `mechanism_claims`, `writing_cards`, `figure_data_points`
- **混合检索**: 0.65 * lexical + 0.35 * semantic (DeterministicEmbeddingService)
- **跨类型去重**: `_global_dedup()`

### External Analysis Service
- **文件**: `app/services/external_analysis_service.py`
- **类**: `ExternalAnalysisService(session, settings)`
- **核心方法**:
  - `import_run(paper_id, source, source_label, raw_text, raw_payload)` → 创建 `ExternalAnalysisRun` + candidates
  - `materialize_candidates(run_id, candidate_ids, created_by)` → 将候选条目物化为 notes/corrections/relationships
  - `list_candidates(run_id)` → 列出候选条目
- **规范化**: 自动将自由文本/JSON 规范化为 `ExternalAnalysisNormalizedModel`（包含 review_notes, correction_proposals, supporting_papers, unmapped_items）
- **LLM 规范化**: 如果输入不是标准结构，会调用 `LLMService.structured_extract()` 自动转换
- **属性**: `self.llm = LLMService(settings)` — 内部已有 LLM 客户端

### External Analysis API
- **文件**: `app/api/external_analysis.py`
- **关键函数**: `_build_internal_ai_review_blob(detail)` — 将论文详情构建为审阅数据包 JSON（第44-81行）
  - 包含: paper 元数据 + comprehensive_analysis + dft_settings_items + catalyst_samples_items + dft_results_items + electrochemical_performance_items + mechanism_claims_items + writing_cards_items + references + relationships + section_excerpts
- **辅助函数**: `_truncate(text, limit)` — 截断长文本；`_sanitize_internal_corrections(normalized)` — 清洗修正的 target_path
- **内部AI审阅端点**: `POST /api/external-analysis/papers/{paper_id}/internal-parse`（第168-253行）
  - 调用链: `PaperQueryService.get_paper_detail()` → `_build_internal_ai_review_blob()` → `LLMService.structured_extract()` → `ExternalAnalysisService.import_run()` → 可选 auto_apply

### LLM 配置
- **文件**: `app/config.py`
- **当前配置** (`.env`):
  ```
  LITAI_WRITER_BACKEND=openai_compatible
  LITAI_WRITER_MODEL=deepseek-v4-flash
  LITAI_WRITER_API_BASE=https://api.deepseek.com
  LITAI_WRITER_API_KEY=sk-xxx
  ```
- **LLMService**: 使用 `openai` Python SDK，`writer_api_base` + `writer_api_key` 配置

### Paper Query Service
- **文件**: `app/services/paper_query.py`
- **核心方法**: `get_paper_detail(paper_id: UUID)` → 返回完整的论文详情模型

### 数据库 Session 管理
- **文件**: `app/db/session.py`
- **工具函数**: `session_scope(database_url)` — 上下文管理器，MCP 工具中统一使用此方式

---

## 需要新增的 4 个 MCP 工具

### 工具 1: `retrieve_evidence` (P0 — 最高优先级)

让外部 AI 按语义检索跨论文的结构化证据。

```python
@mcp_server.tool(name="retrieve_evidence", description="Semantic search across parsed papers for structured evidence (DFT results, mechanism claims, electrochemical data, writing cards, sections, figure data points). Use this to find relevant evidence across multiple papers by topic.")
def retrieve_evidence(
    query: str,                                    # 自然语言查询，如 "oxygen reduction reaction catalyst DFT"
    paper_ids: list[str] | None = None,            # 限定论文ID范围
    evidence_types: list[str] | None = None,       # 限定证据类型，可选: sections, dft_results, electrochemical_performance, mechanism_claims, writing_cards, figure_data_points
    limit_per_type: int = 5,                       # 每类最多返回条数
    target_paper_type: str | None = None,          # 论文分类过滤，如 A1, B2, C3, R
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        retriever = Retriever(session, embedding_dimension=settings.embedding_dimension)
        # 将 paper_ids 字符串列表转为 UUID
        uuid_paper_ids = [UUID(pid) for pid in paper_ids] if paper_ids else None
        # 调用 retriever
        result = retriever.retrieve(
            query=query,
            paper_ids=uuid_paper_ids,
            limit_per_type=limit_per_type,
            target_paper_type=target_paper_type,
        )
        # 如果指定了 evidence_types，只返回对应类型
        if evidence_types:
            valid_types = {"sections", "dft_results", "electrochemical_performance", "mechanism_claims", "writing_cards", "figure_data_points"}
            filtered = {k: v for k, v in result.items() if k in evidence_types and k in valid_types}
            return {"evidence_types_requested": evidence_types, "results": filtered}
        return {"results": result}
```

**新增 import**: `from app.rag.retriever import Retriever`

### 工具 2: `review_paper` (P0)

让外部 AI 触发对单篇论文的深度审阅。复用 `external_analysis.py` 中已有的审阅逻辑。

**重要**: `_build_internal_ai_review_blob` 目前在 `app/api/external_analysis.py` 中是一个模块级私有函数。需要将其提取为共享函数，供 MCP 和 API 共同使用。

方案：将 `_build_internal_ai_review_blob` 和 `_truncate` 和 `_sanitize_internal_corrections` 移到 `app/services/external_analysis_service.py` 作为模块级函数，然后在 `external_analysis.py` 中 import 回来。MCP server 也 import 使用。

```python
@mcp_server.tool(name="review_paper", description="Trigger an AI-powered deep review of a paper using the configured LLM (e.g. DeepSeek). The AI will analyze the paper's extracted data and produce structured review notes, correction proposals, and relationship suggestions. Results are stored as candidates and can be materialized later.")
async def review_paper(
    paper_id: str,
    auto_apply: bool = False,                      # 是否自动应用修正（危险！默认 False）
    source_label: str = "mcp_review",              # 审阅来源标识
) -> dict[str, Any]:
    require_mcp_capability("propose_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        detail = PaperQueryService(session).get_paper_detail(UUID(paper_id))
        if not detail:
            raise ValueError("Paper not found")

        service = ExternalAnalysisService(session=session, settings=settings)
        if not service.llm.is_configured():
            raise ValueError("Internal AI is not configured. Set LITAI_WRITER_API_KEY and LITAI_WRITER_API_BASE.")

        # 构建审阅数据包（从 external_analysis_service import 共享函数）
        review_blob = build_internal_ai_review_blob(detail)

        system_prompt = (
            "You are an internal scientific curation agent for a literature database. "
            "Review the provided parsed-paper bundle and return only high-confidence structured output. "
            "Use review_notes for useful summaries or caveats, correction_proposals for concrete field fixes, "
            "and supporting_papers only when an existing linked paper can be inferred from DOI/title clues already present. "
            "Do not invent evidence, identifiers, values, or target paths. Prefer leaving arrays empty over guessing. "
            "For top-level paper fields, only use these correction field_name values: doi, title, year, journal, authors, abstract, oa_status, license. "
            "For those top-level fields, set target_path exactly equal to field_name. "
            "For structured corrections, only use field_name values from dft_results, mechanism_claims, electrochemical_performance, catalyst_samples, dft_settings, writing_cards, "
            "and set target_path strictly as <collection>:<row_id>:<field> using row ids that already exist in the provided bundle."
        )
        user_prompt = (
            "Analyze this parsed literature record and extraction output. "
            "Identify any clear normalization notes, corrections, and supporting-paper relationships.\n\n"
            f"{review_blob}"
        )

        normalized = await run_in_threadpool(
            service.llm.structured_extract, system_prompt, user_prompt, ExternalAnalysisNormalizedModel
        )
        if normalized is None:
            raise ValueError("AI review failed to produce structured output")

        # 清洗 correction 的 target_path
        normalized = sanitize_internal_corrections(normalized)

        run = service.import_run(
            paper_id=UUID(paper_id),
            source="mcp_review",
            source_label=source_label,
            raw_text=None,
            raw_payload=normalized.model_dump(mode="json"),
        )

        created_notes = 0
        created_corrections = 0
        created_relationships = 0
        auto_applied_corrections = 0
        skipped_candidates = 0

        if auto_apply:
            materialized = service.materialize_candidates(
                run_id=run.id,
                candidate_ids=None,
                created_by="mcp_review",
            )
            created_notes = materialized.created_notes
            created_corrections = materialized.created_corrections
            created_relationships = materialized.created_relationships
            skipped_candidates = materialized.skipped_candidates
            if materialized.created_corrections:
                reviewer = ReviewService(session)
                correction_candidate_ids = [
                    item.materialized_target_id
                    for item in service.list_candidates(run.id)
                    if item.materialized_target_type == "paper_correction" and item.materialized_target_id
                ]
                for correction_id in correction_candidate_ids:
                    try:
                        reviewer.approve_correction(UUID(str(correction_id)), reviewer="mcp_review")
                        auto_applied_corrections += 1
                    except ValueError:
                        continue

        session.commit()
        return {
            "run_id": str(run.id),
            "mapping_status": run.mapping_status,
            "created_notes": created_notes,
            "created_corrections": created_corrections,
            "created_relationships": created_relationships,
            "auto_applied_corrections": auto_applied_corrections,
            "skipped_candidates": skipped_candidates,
        }
```

**新增 import**: `from app.services.external_analysis_service import ExternalAnalysisService, ExternalAnalysisNormalizedModel, build_internal_ai_review_blob, sanitize_internal_corrections`、`from app.services.review_service import ReviewService`

### 工具 3: `import_analysis` (P1)

让外部 AI（如 Cursor 对话）直接把自己的分析结果写回知识库。

```python
@mcp_server.tool(name="import_analysis", description="Import analysis results from an external AI agent (e.g. Cursor, DeepSeek chat, Claude) into the library. Supports free-text or structured JSON. The system will auto-normalize the input into structured notes, corrections, and relationships.")
def import_analysis(
    paper_id: str,
    source: str,                                   # 来源标识，如 "cursor", "deepseek-chat", "claude-web"
    source_label: str = "",                        # 显示名
    raw_text: str | None = None,                   # 自由文本分析
    raw_payload: dict | None = None,               # 结构化 JSON（符合 ExternalAnalysisNormalizedModel 格式则直接解析，否则 LLM 规范化）
) -> dict[str, Any]:
    require_mcp_capability("propose_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        service = ExternalAnalysisService(session=session, settings=settings)
        run = service.import_run(
            paper_id=UUID(paper_id),
            source=source,
            source_label=source_label or source,
            raw_text=raw_text,
            raw_payload=raw_payload,
        )
        candidates = service.list_candidates(run.id)
        session.commit()
        return {
            "run_id": str(run.id),
            "mapping_status": run.mapping_status,
            "mapping_error": run.mapping_error,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "id": str(c.id),
                    "type": c.candidate_type,
                    "confidence": c.confidence,
                    "status": c.status,
                    "summary": (c.normalized_payload or {}).get("content") or (c.normalized_payload or {}).get("reason", ""),
                }
                for c in candidates
            ],
        }
```

### 工具 4: `compare_papers` (P2)

让外部 AI 对比多篇论文的抽取结果。

```python
@mcp_server.tool(name="compare_papers", description="Compare extracted data across multiple papers side-by-side. Returns structured results for DFT settings, catalyst samples, performance metrics, mechanism claims, etc. Useful for finding contradictions or confirming trends.")
def compare_papers(
    paper_ids: list[str],                          # 要对比的论文 ID 列表（2-10 篇）
    fields: list[str] | None = None,               # 限定比较的字段，可选: dft_settings, catalyst_samples, dft_results, electrochemical_performance, mechanism_claims, writing_cards
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    if len(paper_ids) < 2 or len(paper_ids) > 10:
        raise ValueError("Must compare 2-10 papers")

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        query_service = PaperQueryService(session)
        papers_data = []
        for pid in paper_ids:
            detail = query_service.get_paper_detail(UUID(pid))
            if not detail:
                raise ValueError(f"Paper {pid} not found")
            papers_data.append({
                "id": str(detail.id),
                "title": detail.title,
                "year": detail.year,
                "journal": detail.journal,
                "paper_type": detail.comprehensive_analysis.get("paper_type") if detail.comprehensive_analysis else None,
                "dft_settings": [item.model_dump(mode="json") for item in detail.dft_settings_items[:20]],
                "catalyst_samples": [item.model_dump(mode="json") for item in detail.catalyst_samples_items[:20]],
                "dft_results": [item.model_dump(mode="json") for item in detail.dft_results_items[:30]],
                "electrochemical_performance": [item.model_dump(mode="json") for item in detail.electrochemical_performance_items[:20]],
                "mechanism_claims": [item.model_dump(mode="json") for item in detail.mechanism_claims_items[:20]],
                "writing_cards": [item.model_dump(mode="json") for item in detail.writing_cards_items[:15]],
            })

        # 如果指定了 fields，只返回对应字段
        valid_fields = {"dft_settings", "catalyst_samples", "dft_results", "electrochemical_performance", "mechanism_claims", "writing_cards"}
        if fields:
            active_fields = set(fields) & valid_fields
        else:
            active_fields = valid_fields

        comparison = []
        for p in papers_data:
            entry = {"id": p["id"], "title": p["title"], "year": p["year"], "paper_type": p["paper_type"]}
            for f in active_fields:
                entry[f] = p.get(f, [])
            comparison.append(entry)

        return {
            "paper_count": len(comparison),
            "compared_fields": sorted(active_fields),
            "papers": comparison,
        }
```

---

## 实施步骤

### Step 1: 提取共享函数

将 `app/api/external_analysis.py` 中的三个私有函数移到 `app/services/external_analysis_service.py` 作为模块级公共函数：

1. 复制 `_truncate`、`_build_internal_ai_review_blob`、`_sanitize_internal_corrections` 到 `external_analysis_service.py` 末尾
2. 重命名（去掉下划线前缀，变为公共函数）：
   - `_truncate` → 保持 `_truncate`（仍为内部辅助）
   - `_build_internal_ai_review_blob` → `build_internal_ai_review_blob`
   - `_sanitize_internal_corrections` → `sanitize_internal_corrections`
3. 在 `external_analysis.py` 中改为 import：
   ```python
   from app.services.external_analysis_service import (
       ExternalAnalysisService,
       ExternalAnalysisNormalizedModel,
       _truncate,
       build_internal_ai_review_blob as _build_internal_ai_review_blob,
       sanitize_internal_corrections as _sanitize_internal_corrections,
   )
   ```
4. 运行测试确认 `/api/external-analysis/papers/{id}/internal-parse` 端点仍正常工作

### Step 2: 在 `server.py` 新增 4 个工具

按上面的代码模板，在 `app/mcp/server.py` 末尾依次添加 4 个工具函数。

需要新增的 import：
```python
from app.rag.retriever import Retriever
from app.services.external_analysis_service import (
    ExternalAnalysisService,
    ExternalAnalysisNormalizedModel,
    build_internal_ai_review_blob,
    sanitize_internal_corrections,
)
from app.services.review_service import ReviewService
```

### Step 3: 编写测试

新增测试文件 `tests/test_mcp_new_tools.py`，覆盖：
1. `retrieve_evidence` — mock Retriever，验证参数传递和 evidence_types 过滤
2. `review_paper` — mock ExternalAnalysisService + LLMService，验证审阅流程
3. `import_analysis` — mock ExternalAnalysisService，验证 import_run 被正确调用
4. `compare_papers` — mock PaperQueryService，验证字段过滤和边界检查

### Step 4: 验证

1. 运行全量测试：`cd D:/Desktop/03_代码与开发/爬虫脚步下载文章/literature-ai/backend && python -m pytest tests/ -v`（当前基线 150/150）
2. 启动后端 `uvicorn app.main:app`
3. 用 MCP 客户端连接 `http://localhost:8000/mcp` 测试各工具调用

---

## 客户端配置参考（实施完成后提供给用户）

### Cursor
```json
// .cursor/mcp.json
{
  "mcpServers": {
    "literature-ai": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

### VS Code Copilot / Codex
```json
// .vscode/mcp.json
{
  "servers": {
    "literature-ai": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

### Claude Desktop
```json
// claude_desktop_config.json
{
  "mcpServers": {
    "literature-ai": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

MCP 密钥配置（在 `.env` 中）：
```
LITAI_MCP_API_KEYS=cursor|Cursor|sk-cursor-key|read_papers,request_parse,append_notes,propose_corrections;vscode|VS Code|sk-vscode-key|read_papers,request_parse,append_notes,propose_corrections
```

---

## 注意事项

1. **最小改动原则** — 不要大范围重写现有代码，只新增工具和提取共享函数
2. **向后兼容** — 所有新参数都有默认值，现有 API 端点和 MCP 工具不受影响
3. **auto_apply 默认 False** — 外部 AI 的修正提案默认不自动应用，需人工审批
4. **`_build_internal_ai_review_blob` 提取后，`external_analysis.py` 的 `internal_ai_parse_paper` 端点必须继续正常工作** — 这是最容易出问题的地方，务必测试
5. **MCP 工具中的 session 管理** — 遵循现有模式：`with session_scope(settings.database_url) as session:` + 手动 `session.commit()`
6. **review_paper 是 async** — 因为 `LLMService.structured_extract` 是同步的，需要 `await run_in_threadpool()` 包装（参考已有的 `parse_paper` 工具写法）
7. **测试** — 运行 `cd D:/Desktop/03_代码与开发/爬虫脚步下载文章/literature-ai/backend && python -m pytest tests/ -v` 确认全量通过（当前基线 150/150）

## 用户原话

> 1.内部AI指接入DeepSeek等API，2.IDE中的AI指Codex、Cursor、VS Code中的AI，3.我想让这些AI直接查询知识库，主动分析文献，因为我不相信软件自己分析的准确度
