# 文献检索性能优化执行计划报告

**生成日期**: 2026-05-20  
**项目**: Literature AI  
**目标**: 分析检索慢原因 + 实时刷新方案

---

## 一、现状诊断

### 1.1 检索慢的原因分析

| 瓶颈 | 严重程度 | 位置 | 影响 |
|------|---------|------|------|
| Retriever 全表扫描 | 🔴 严重 | `backend/app/rag/retriever.py` | 5张表 SELECT * + Python内存评分，O(N)线性增长 |
| 9次COUNT查询 | 🟡 中等 | `backend/app/services/paper_query.py:101-122` | 每次列表请求10次DB往返 |
| ILIKE全表扫描 | 🟡 中等 | `backend/app/services/paper_query.py:47-71` | 关键词搜索无法利用索引 |
| 入库同步阻塞 | 🟡 中等 | `backend/app/services/paper_ingestion.py:125-208` | LLM调用阻塞event loop |
| 向量检索无索引 | 🟡 中等 | `backend/app/db/models.py` | pgvector ANN索引未建立 |

### 1.2 为什么不能实时刷新

**前端问题**:
```javascript
// 当前代码：只有手动触发，无任何自动机制
document.getElementById('searchInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') fetchPapers();
});
// 缺失: setInterval / WebSocket / SSE
```

**后端问题**:
```python
# 当前代码：无推送机制
app = FastAPI(...)
app.include_router(papers_router, ...)
# 缺失: WebSocket / SSE endpoint
```

**同步阻塞问题**:
```python
# 入库操作必须等所有LLM完成后才能返回
self.extraction_pipeline.run_stage2(paper, document)  # ← 同步阻塞
self.session.commit()  # ← 之后才返回
```

---

## 二、技术方案（不改代码）

### 2.1 解决检索慢

#### 方案A: 添加数据库索引
- `papers` 表: `title`, `abstract`, `journal` 添加 GIN/TSVECTOR 索引
- `paper_sections.text` 添加全文搜索索引
- pgvector 字段添加 HNSW/IVFFlat 索引

#### 方案B: Retriever 优化
- 移除全表扫描，改为带 `paper_ids` 过滤的分页查询
- 将 Python 内存评分迁移到 SQL 层

#### 方案C: 列表查询优化
- 用单个 JOIN 查询替代 9 次 COUNT
- 添加 `paper_ids` 索引

### 2.2 解决实时刷新

#### 方案A: 前端轮询（最简方案）
```javascript
// 添加定时轮询
setInterval(() => {
    if (!isSearching) fetchPapers();
}, 5000);  // 5秒刷新
```

#### 方案B: SSE推送（推荐方案）
```python
# 后端添加 SSE endpoint
@app.get("/api/papers/stream")
async def stream_papers():
    async def event_generator():
        while True:
            # 推送最新论文列表
            yield {"data": await get_papers()}
            await asyncio.sleep(5)
```

#### 方案C: WebSocket（高级方案）
- 双向通信，可推送入库进度
- 复杂度高，适合长期架构

### 2.3 解决入库阻塞

#### 方案A: Celery后台任务（推荐）
- 当前 Celery worker 已定义但未使用
- 将 `ingest_pdf()` 改为后台任务
- 完成后通过 SSE/WebSocket 通知前端

#### 方案B: 分阶段提交
- 先快速入库元数据（立即可用）
- LLM分析异步完成后再更新sections/tables

---

## 三、实施优先级

### Phase 1: 快速见效（1-2天）
1. 添加数据库索引（ILIKE → GIN索引）
2. 移除 Retriever 全表扫描
3. 列表查询9次COUNT → 1次JOIN

### Phase 2: 实时刷新（2-3天）
1. 前端添加轮询机制
2. 后端添加 SSE endpoint
3. 入库进度推送

### Phase 3: 长期优化（持续）
1. pgvector ANN 索引
2. RAG embedding 质量提升
3. 向量检索迁移到数据库层

---

## 四、风险评估

| 方案 | 风险 | 缓解措施 |
|------|------|---------|
| 添加索引 | 生产环境加索引可能锁表 | 使用 `CREATE INDEX CONCURRENTLY` |
| SSE推送 | 多用户连接资源消耗 | 连接数限制 + 心跳保活 |
| Celery迁移 | 任务状态管理复杂 | 详细日志 + 幂等设计 |

---

## 五、不改代码说明

本报告仅分析原因和提供方案思路，不涉及代码修改。实际实施时需：

1. **索引操作**: 在数据库端直接执行 SQL
2. **前端轮询**: 需修改 `frontend/pages/literature_library/index.html`
3. **SSE推送**: 需修改 `backend/app/main.py` 和相关路由
4. **Celery任务**: 需修改 API 路由调用方式

---

**报告结束**
> 历史归档说明：此文件为历史分析文档，存在阶段性假设，仅作历史参考，不作为当前执行依据。
