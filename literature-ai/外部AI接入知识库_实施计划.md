# 澶栭儴 AI 鎺ュ叆鐭ヨ瘑搴?鈥?瀹炴柦璁″垝

## 浠诲姟鐩爣

鍦?literature-ai 椤圭洰鐨?MCP 鏈嶅姟绔柊澧?4 涓伐鍏凤紝璁╁閮?AI锛圖eepSeek API銆丆ursor銆乂S Code Copilot銆丆odex 绛夛級鑳藉鐩存帴鏌ヨ鐭ヨ瘑搴撱€佹绱㈣法璁烘枃璇佹嵁銆佽Е鍙?AI 瀹￠槄銆佸鍏ュ垎鏋愮粨鏋溿€?

**鐢ㄦ埛鏍稿績璇夋眰**锛氫笉鐩镐俊杞欢鑷繁鍒嗘瀽鐨勫噯纭害锛屾兂璁╁閮?AI 鏉ュ鏍稿拰琛ュ厖銆?

## 椤圭洰浣嶇疆

```
cd literature-ai/backend
```

---

## 鐜版湁鏋舵瀯锛堜笉瑕佹敼锛屽彧闇€鍩轰簬瀹冩墿灞曪級

### MCP 鏈嶅姟绔?
- **鏂囦欢**: `app/mcp/server.py`
- **妗嗘灦**: `FastMCP` (mcp Python SDK)锛宍streamable_http` 妯″紡
- **鎸傝浇**: `app/main.py` 绗?8琛?`app.mount("/mcp", mcp_http_app)`
- **宸叉湁 13 涓伐鍏?*: `query_papers`, `scan_local_pdfs`, `get_paper`, `list_notes`, `append_note`, `propose_correction`, `get_parse_status`, `get_correction_queue`, `get_correction_detail`, `approve_correction`, `reject_correction`, `parse_paper`, `ingest_pdf_batch`
- **璁よ瘉**: Bearer Token锛屽瘑閽ユ牸寮?`source_prefix|display_name|api_key|capability1,capability2`锛岄厤缃湪 `LITAI_MCP_API_KEYS` 鐜鍙橀噺
- **鏉冮檺闆嗗悎**: `read_papers`, `request_parse`, `append_notes`, `propose_corrections`, `review_corrections`
- **姣忎釜宸ュ叿寮€澶撮兘璋冪敤 `require_mcp_capability("xxx")`** 鍋氭潈闄愭鏌?

### RAG Retriever
- **鏂囦欢**: `app/rag/retriever.py`
- **绫?*: `Retriever(session, embedding_dimension=64)`
- **鏍稿績鏂规硶**: `retrieve(query, paper_ids, limit_per_type, target_paper_type, paper_type_filter)` 鈫?杩斿洖鍏被璇佹嵁 dict:
  - `sections`, `dft_results`, `electrochemical_performance`, `mechanism_claims`, `writing_cards`, `figure_data_points`
- **娣峰悎妫€绱?*: 0.65 * lexical + 0.35 * semantic (DeterministicEmbeddingService)
- **璺ㄧ被鍨嬪幓閲?*: `_global_dedup()`

### External Analysis Service
- **鏂囦欢**: `app/services/external_analysis_service.py`
- **绫?*: `ExternalAnalysisService(session, settings)`
- **鏍稿績鏂规硶**:
  - `import_run(paper_id, source, source_label, raw_text, raw_payload)` 鈫?鍒涘缓 `ExternalAnalysisRun` + candidates
  - `materialize_candidates(run_id, candidate_ids, created_by)` 鈫?灏嗗€欓€夋潯鐩墿鍖栦负 notes/corrections/relationships
  - `list_candidates(run_id)` 鈫?鍒楀嚭鍊欓€夋潯鐩?
- **瑙勮寖鍖?*: 鑷姩灏嗚嚜鐢辨枃鏈?JSON 瑙勮寖鍖栦负 `ExternalAnalysisNormalizedModel`锛堝寘鍚?review_notes, correction_proposals, supporting_papers, unmapped_items锛?
- **LLM 瑙勮寖鍖?*: 濡傛灉杈撳叆涓嶆槸鏍囧噯缁撴瀯锛屼細璋冪敤 `LLMService.structured_extract()` 鑷姩杞崲
- **灞炴€?*: `self.llm = LLMService(settings)` 鈥?鍐呴儴宸叉湁 LLM 瀹㈡埛绔?

### External Analysis API
- **鏂囦欢**: `app/api/external_analysis.py`
- **鍏抽敭鍑芥暟**: `_build_internal_ai_review_blob(detail)` 鈥?灏嗚鏂囪鎯呮瀯寤轰负瀹￠槄鏁版嵁鍖?JSON锛堢44-81琛岋級
  - 鍖呭惈: paper 鍏冩暟鎹?+ comprehensive_analysis + dft_settings_items + catalyst_samples_items + dft_results_items + electrochemical_performance_items + mechanism_claims_items + writing_cards_items + references + relationships + section_excerpts
- **杈呭姪鍑芥暟**: `_truncate(text, limit)` 鈥?鎴柇闀挎枃鏈紱`_sanitize_internal_corrections(normalized)` 鈥?娓呮礂淇鐨?target_path
- **鍐呴儴AI瀹￠槄绔偣**: `POST /api/external-analysis/papers/{paper_id}/internal-parse`锛堢168-253琛岋級
  - 璋冪敤閾? `PaperQueryService.get_paper_detail()` 鈫?`_build_internal_ai_review_blob()` 鈫?`LLMService.structured_extract()` 鈫?`ExternalAnalysisService.import_run()` 鈫?鍙€?auto_apply

### LLM 閰嶇疆
- **鏂囦欢**: `app/config.py`
- **褰撳墠閰嶇疆** (`.env`):
  ```
  LITAI_WRITER_BACKEND=openai_compatible
  LITAI_WRITER_MODEL=deepseek-v4-flash
  LITAI_WRITER_API_BASE=https://api.deepseek.com
  LITAI_WRITER_API_KEY=sk-xxx
  ```
- **LLMService**: 浣跨敤 `openai` Python SDK锛宍writer_api_base` + `writer_api_key` 閰嶇疆

### Paper Query Service
- **鏂囦欢**: `app/services/paper_query.py`
- **鏍稿績鏂规硶**: `get_paper_detail(paper_id: UUID)` 鈫?杩斿洖瀹屾暣鐨勮鏂囪鎯呮ā鍨?

### 鏁版嵁搴?Session 绠＄悊
- **鏂囦欢**: `app/db/session.py`
- **宸ュ叿鍑芥暟**: `session_scope(database_url)` 鈥?涓婁笅鏂囩鐞嗗櫒锛孧CP 宸ュ叿涓粺涓€浣跨敤姝ゆ柟寮?

---

## 闇€瑕佹柊澧炵殑 4 涓?MCP 宸ュ叿

### 宸ュ叿 1: `retrieve_evidence` (P0 鈥?鏈€楂樹紭鍏堢骇)

璁╁閮?AI 鎸夎涔夋绱㈣法璁烘枃鐨勭粨鏋勫寲璇佹嵁銆?

```python
@mcp_server.tool(name="retrieve_evidence", description="Semantic search across parsed papers for structured evidence (DFT results, mechanism claims, electrochemical data, writing cards, sections, figure data points). Use this to find relevant evidence across multiple papers by topic.")
def retrieve_evidence(
    query: str,                                    # 鑷劧璇█鏌ヨ锛屽 "oxygen reduction reaction catalyst DFT"
    paper_ids: list[str] | None = None,            # 闄愬畾璁烘枃ID鑼冨洿
    evidence_types: list[str] | None = None,       # 闄愬畾璇佹嵁绫诲瀷锛屽彲閫? sections, dft_results, electrochemical_performance, mechanism_claims, writing_cards, figure_data_points
    limit_per_type: int = 5,                       # 姣忕被鏈€澶氳繑鍥炴潯鏁?
    target_paper_type: str | None = None,          # 璁烘枃鍒嗙被杩囨护锛屽 A1, B2, C3, R
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        retriever = Retriever(session, embedding_dimension=settings.embedding_dimension)
        # 灏?paper_ids 瀛楃涓插垪琛ㄨ浆涓?UUID
        uuid_paper_ids = [UUID(pid) for pid in paper_ids] if paper_ids else None
        # 璋冪敤 retriever
        result = retriever.retrieve(
            query=query,
            paper_ids=uuid_paper_ids,
            limit_per_type=limit_per_type,
            target_paper_type=target_paper_type,
        )
        # 濡傛灉鎸囧畾浜?evidence_types锛屽彧杩斿洖瀵瑰簲绫诲瀷
        if evidence_types:
            valid_types = {"sections", "dft_results", "electrochemical_performance", "mechanism_claims", "writing_cards", "figure_data_points"}
            filtered = {k: v for k, v in result.items() if k in evidence_types and k in valid_types}
            return {"evidence_types_requested": evidence_types, "results": filtered}
        return {"results": result}
```

**鏂板 import**: `from app.rag.retriever import Retriever`

### 宸ュ叿 2: `review_paper` (P0)

璁╁閮?AI 瑙﹀彂瀵瑰崟绡囪鏂囩殑娣卞害瀹￠槄銆傚鐢?`external_analysis.py` 涓凡鏈夌殑瀹￠槄閫昏緫銆?

**閲嶈**: `_build_internal_ai_review_blob` 鐩墠鍦?`app/api/external_analysis.py` 涓槸涓€涓ā鍧楃骇绉佹湁鍑芥暟銆傞渶瑕佸皢鍏舵彁鍙栦负鍏变韩鍑芥暟锛屼緵 MCP 鍜?API 鍏卞悓浣跨敤銆?

鏂规锛氬皢 `_build_internal_ai_review_blob` 鍜?`_truncate` 鍜?`_sanitize_internal_corrections` 绉诲埌 `app/services/external_analysis_service.py` 浣滀负妯″潡绾у嚱鏁帮紝鐒跺悗鍦?`external_analysis.py` 涓?import 鍥炴潵銆侻CP server 涔?import 浣跨敤銆?

```python
@mcp_server.tool(name="review_paper", description="Trigger an AI-powered deep review of a paper using the configured LLM (e.g. DeepSeek). The AI will analyze the paper's extracted data and produce structured review notes, correction proposals, and relationship suggestions. Results are stored as candidates and can be materialized later.")
async def review_paper(
    paper_id: str,
    auto_apply: bool = False,                      # 鏄惁鑷姩搴旂敤淇锛堝嵄闄╋紒榛樿 False锛?
    source_label: str = "mcp_review",              # 瀹￠槄鏉ユ簮鏍囪瘑
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

        # 鏋勫缓瀹￠槄鏁版嵁鍖咃紙浠?external_analysis_service import 鍏变韩鍑芥暟锛?
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

        # 娓呮礂 correction 鐨?target_path
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

**鏂板 import**: `from app.services.external_analysis_service import ExternalAnalysisService, ExternalAnalysisNormalizedModel, build_internal_ai_review_blob, sanitize_internal_corrections`銆乣from app.services.review_service import ReviewService`

### 宸ュ叿 3: `import_analysis` (P1)

璁╁閮?AI锛堝 Cursor 瀵硅瘽锛夌洿鎺ユ妸鑷繁鐨勫垎鏋愮粨鏋滃啓鍥炵煡璇嗗簱銆?

```python
@mcp_server.tool(name="import_analysis", description="Import analysis results from an external AI agent (e.g. Cursor, DeepSeek chat, Claude) into the library. Supports free-text or structured JSON. The system will auto-normalize the input into structured notes, corrections, and relationships.")
def import_analysis(
    paper_id: str,
    source: str,                                   # 鏉ユ簮鏍囪瘑锛屽 "cursor", "deepseek-chat", "claude-web"
    source_label: str = "",                        # 鏄剧ず鍚?
    raw_text: str | None = None,                   # 鑷敱鏂囨湰鍒嗘瀽
    raw_payload: dict | None = None,               # 缁撴瀯鍖?JSON锛堢鍚?ExternalAnalysisNormalizedModel 鏍煎紡鍒欑洿鎺ヨВ鏋愶紝鍚﹀垯 LLM 瑙勮寖鍖栵級
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

### 宸ュ叿 4: `compare_papers` (P2)

璁╁閮?AI 瀵规瘮澶氱瘒璁烘枃鐨勬娊鍙栫粨鏋溿€?

```python
@mcp_server.tool(name="compare_papers", description="Compare extracted data across multiple papers side-by-side. Returns structured results for DFT settings, catalyst samples, performance metrics, mechanism claims, etc. Useful for finding contradictions or confirming trends.")
def compare_papers(
    paper_ids: list[str],                          # 瑕佸姣旂殑璁烘枃 ID 鍒楄〃锛?-10 绡囷級
    fields: list[str] | None = None,               # 闄愬畾姣旇緝鐨勫瓧娈碉紝鍙€? dft_settings, catalyst_samples, dft_results, electrochemical_performance, mechanism_claims, writing_cards
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

        # 濡傛灉鎸囧畾浜?fields锛屽彧杩斿洖瀵瑰簲瀛楁
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

## 瀹炴柦姝ラ

### Step 1: 鎻愬彇鍏变韩鍑芥暟

灏?`app/api/external_analysis.py` 涓殑涓変釜绉佹湁鍑芥暟绉诲埌 `app/services/external_analysis_service.py` 浣滀负妯″潡绾у叕鍏卞嚱鏁帮細

1. 澶嶅埗 `_truncate`銆乣_build_internal_ai_review_blob`銆乣_sanitize_internal_corrections` 鍒?`external_analysis_service.py` 鏈熬
2. 閲嶅懡鍚嶏紙鍘绘帀涓嬪垝绾垮墠缂€锛屽彉涓哄叕鍏卞嚱鏁帮級锛?
   - `_truncate` 鈫?淇濇寔 `_truncate`锛堜粛涓哄唴閮ㄨ緟鍔╋級
   - `_build_internal_ai_review_blob` 鈫?`build_internal_ai_review_blob`
   - `_sanitize_internal_corrections` 鈫?`sanitize_internal_corrections`
3. 鍦?`external_analysis.py` 涓敼涓?import锛?
   ```python
   from app.services.external_analysis_service import (
       ExternalAnalysisService,
       ExternalAnalysisNormalizedModel,
       _truncate,
       build_internal_ai_review_blob as _build_internal_ai_review_blob,
       sanitize_internal_corrections as _sanitize_internal_corrections,
   )
   ```
4. 杩愯娴嬭瘯纭 `/api/external-analysis/papers/{id}/internal-parse` 绔偣浠嶆甯稿伐浣?

### Step 2: 鍦?`server.py` 鏂板 4 涓伐鍏?

鎸変笂闈㈢殑浠ｇ爜妯℃澘锛屽湪 `app/mcp/server.py` 鏈熬渚濇娣诲姞 4 涓伐鍏峰嚱鏁般€?

闇€瑕佹柊澧炵殑 import锛?
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

### Step 3: 缂栧啓娴嬭瘯

鏂板娴嬭瘯鏂囦欢 `tests/test_mcp_new_tools.py`锛岃鐩栵細
1. `retrieve_evidence` 鈥?mock Retriever锛岄獙璇佸弬鏁颁紶閫掑拰 evidence_types 杩囨护
2. `review_paper` 鈥?mock ExternalAnalysisService + LLMService锛岄獙璇佸闃呮祦绋?
3. `import_analysis` 鈥?mock ExternalAnalysisService锛岄獙璇?import_run 琚纭皟鐢?
4. `compare_papers` 鈥?mock PaperQueryService锛岄獙璇佸瓧娈佃繃婊ゅ拰杈圭晫妫€鏌?

### Step 4: 楠岃瘉

1. 杩愯鍏ㄩ噺娴嬭瘯锛歚cd literature-ai/backend && python -m pytest tests/ -v`锛堝綋鍓嶅熀绾?150/150锛?
2. 鍚姩鍚庣 `uvicorn app.main:app`
3. 鐢?MCP 瀹㈡埛绔繛鎺?`http://localhost:8000/mcp` 娴嬭瘯鍚勫伐鍏疯皟鐢?

---

## 瀹㈡埛绔厤缃弬鑰冿紙瀹炴柦瀹屾垚鍚庢彁渚涚粰鐢ㄦ埛锛?

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

MCP 瀵嗛挜閰嶇疆锛堝湪 `.env` 涓級锛?
```
LITAI_MCP_API_KEYS=cursor|Cursor|sk-cursor-key|read_papers,request_parse,append_notes,propose_corrections;vscode|VS Code|sk-vscode-key|read_papers,request_parse,append_notes,propose_corrections
```

---

## 娉ㄦ剰浜嬮」

1. **鏈€灏忔敼鍔ㄥ師鍒?* 鈥?涓嶈澶ц寖鍥撮噸鍐欑幇鏈変唬鐮侊紝鍙柊澧炲伐鍏峰拰鎻愬彇鍏变韩鍑芥暟
2. **鍚戝悗鍏煎** 鈥?鎵€鏈夋柊鍙傛暟閮芥湁榛樿鍊硷紝鐜版湁 API 绔偣鍜?MCP 宸ュ叿涓嶅彈褰卞搷
3. **auto_apply 榛樿 False** 鈥?澶栭儴 AI 鐨勪慨姝ｆ彁妗堥粯璁や笉鑷姩搴旂敤锛岄渶浜哄伐瀹℃壒
4. **`_build_internal_ai_review_blob` 鎻愬彇鍚庯紝`external_analysis.py` 鐨?`internal_ai_parse_paper` 绔偣蹇呴』缁х画姝ｅ父宸ヤ綔** 鈥?杩欐槸鏈€瀹规槗鍑洪棶棰樼殑鍦版柟锛屽姟蹇呮祴璇?
5. **MCP 宸ュ叿涓殑 session 绠＄悊** 鈥?閬靛惊鐜版湁妯″紡锛歚with session_scope(settings.database_url) as session:` + 鎵嬪姩 `session.commit()`
6. **review_paper 鏄?async** 鈥?鍥犱负 `LLMService.structured_extract` 鏄悓姝ョ殑锛岄渶瑕?`await run_in_threadpool()` 鍖呰锛堝弬鑰冨凡鏈夌殑 `parse_paper` 宸ュ叿鍐欐硶锛?
7. **娴嬭瘯** 鈥?杩愯 `cd literature-ai/backend && python -m pytest tests/ -v` 纭鍏ㄩ噺閫氳繃锛堝綋鍓嶅熀绾?150/150锛?

## 鐢ㄦ埛鍘熻瘽

> 1.鍐呴儴AI鎸囨帴鍏eepSeek绛堿PI锛?.IDE涓殑AI鎸嘋odex銆丆ursor銆乂S Code涓殑AI锛?.鎴戞兂璁╄繖浜汚I鐩存帴鏌ヨ鐭ヨ瘑搴擄紝涓诲姩鍒嗘瀽鏂囩尞锛屽洜涓烘垜涓嶇浉淇¤蒋浠惰嚜宸卞垎鏋愮殑鍑嗙‘搴?



