# 文献检索实时刷新功能 - 修改报告

**日期**: 2026-05-20  
**修改人**: Senior Developer  
**审查人**: Gemini  
**状态**: 待审查

---

## 一、修改概述

根据性能分析报告，实现了**实时刷新功能**，使用 Server-Sent Events (SSE) 技术实现论文列表的实时更新。

### 修改范围

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `backend/app/api/papers.py` | 新增功能 | 添加 SSE endpoint + papers status endpoint |
| `frontend/pages/literature_library/index.html` | 功能增强 | 添加 SSE 监听 + 实时进度显示 |

---

## 二、后端修改详情

### 2.1 新增 SSE Endpoint: `/api/papers/stream`

**路径**: `backend/app/api/papers.py`

```python
@router.get("/stream")
async def stream_papers(...):
    """SSE endpoint for real-time paper list updates."""
```

**功能**:
- 每 3 秒推送一次论文列表更新
- 仅当论文数量变化时才推送 `papers_update` 事件
- 持续发送 `heartbeat` 事件用于保活
- 支持与列表 API 相同的过滤参数

**SSE 事件类型**:
- `papers_update`: 论文列表数据
- `heartbeat`: 心跳包，包含 total 和 displayed 数量
- `error`: 错误信息

### 2.2 新增 Papers Status Endpoint: `/api/papers/status`

```python
@router.get("/status")
async def get_papers_status(...):
    """Get current papers status for polling."""
```

**返回**:
```json
{
    "total": 42,
    "last_added": {
        "id": "uuid",
        "title": "Paper Title",
        "created_at": "2026-05-20T..."
    }
}
```

---

## 三、前端修改详情

### 3.1 新增 SSE 连接管理

**新增变量**:
```javascript
let eventSource = null;       // SSE 连接
let lastPaperCount = 0;       // 上次论文数量
let isImporting = false;      // 是否正在导入
let importStatus = null;      // 导入状态
```

**新增函数**:
- `initSSE()`: 初始化 SSE 连接
- `disconnectSSE()`: 断开 SSE 连接
- `updatePapersFromSSE(papers)`: SSE 数据更新
- `updatePaginationInfo(papers)`: 更新分页信息
- `renderPapers(papers, tbody)`: 渲染论文列表（抽取复用）
- `updateImportProgress()`: 更新导入进度显示

### 3.2 实时进度显示

**导入操作时显示进度条**:
- 位置: 页面右上角浮动显示
- 状态: `📥 Downloading & Processing...` → `✅ Import Complete!`
- 自动消失: 完成后 2 秒自动移除

**支持的导入方式**:
- Upload PDF
- Download by DOI
- Download & Import from online search

### 3.3 实时刷新行为

**页面加载时**:
1. 立即调用 `fetchPapers()` 获取初始数据
2. 启动 SSE 连接监听更新

**搜索行为**:
1. 断开当前 SSE 连接
2. 获取新搜索结果
3. 建立新的 SSE 连接

**数据更新时**:
- 自动刷新论文列表
- 更新论文总数显示

---

## 四、技术实现

### 4.1 SSE vs WebSocket 选择

| 特性 | SSE | WebSocket |
|------|-----|-----------|
| 单向通信 | ✅ | ✅ |
| 实现复杂度 | 低 | 高 |
| HTTP/2 兼容 | ✅ | ✅ |
| 自动重连 | 浏览器自动 | 需手动实现 |
| 本场景适用性 | ✅ (服务器推送为主) | 过度设计 |

**结论**: 本场景只需服务器推送数据，选择 SSE 更轻量。

### 4.2 性能考虑

- SSE 心跳间隔: 3 秒（平衡实时性与服务器负载）
- 数据变化检测: 仅在数量变化时推送完整列表
- 前端无数据变化时静默接受更新

---

## 五、测试要点（审查项）

### 5.1 功能测试

- [ ] SSE 连接是否正常建立
- [ ] 新论文入库后列表是否自动刷新
- [ ] 导入进度显示是否正确
- [ ] 搜索后 SSE 是否正确重连
- [ ] 页面关闭时 SSE 是否正确断开

### 5.2 边界测试

- [ ] 快速连续导入多篇论文
- [ ] 网络断开后重连
- [ ] 长时间保持页面不操作
- [ ] 多标签页同时打开

### 5.3 性能测试

- [ ] SSE 连接内存占用
- [ ] 1000+ 论文时的推送性能
- [ ] 多用户并发连接

---

## 六、已知限制

1. **不显示入库进度细节**: 当前仅显示状态，无法显示 PDF 解析、LLM 分析等具体进度
2. **无离线检测**: 网络断开后需刷新页面重连
3. **仅支持列表页**: 详情页、DFT 数据库页等未添加 SSE

---

## 七、后续优化建议（可选）

1. **入库详细进度**: 可添加 Redis pub/sub 实时推送解析进度
2. **IndexedDB 缓存**: 离线时显示缓存数据
3. **其他页面扩展**: DFT Database、Mechanism Knowledge 等页面添加 SSE
4. **WebSocket 升级**: 若需双向通信可升级为 WebSocket

---

## 八、审查清单

请审查以下内容：

1. **SSE 规范符合性**: 是否正确使用 EventSource 和 SSE 协议
2. **错误处理**: SSE 连接断开、错误时的处理是否完善
3. **资源清理**: SSE 连接是否正确释放
4. **参数传递**: 过滤器参数是否正确传递到 SSE endpoint
5. **前端代码质量**: 是否有潜在 bug 或边界情况遗漏
6. **性能影响**: 3 秒轮询间隔是否合理

---

**修改完成，等待 Gemini 审查**

---

## 九、修改记录

### 2026-05-20 v1.1 - 修复 SSE 格式问题

**修复内容**:
- `stream_papers()` event_generator yield 格式从 dict 改为 SSE 字符串格式
- 确保 SSE 协议格式正确: `event: {type}\ndata: {json}\n\n`

```python
# 修复前 (错误)
yield {"event": "papers_update", "data": json.dumps(...)}

# 修复后 (正确)
yield f"event: papers_update\ndata: {json.dumps(...)}\n\n"
```

### 2026-05-20 v1.2 - 修复严重漏洞

**漏洞1: SSE session 生命周期问题 (严重)**
- **问题**: 原始 session 来自 FastAPI 依赖注入，请求结束后会失效
- **修复**: 使用 `session_scope()` context manager，每次轮询创建新 session
- **代码变更**:
```python
# 修复前
papers = PaperQueryService(session).list_papers(...)

# 修复后
with session_scope(settings.database_url) as poll_session:
    papers = PaperQueryService(poll_session).list_papers(...)
```

**漏洞2: 分页/清除过滤器不重连 SSE (中等)**
- **问题**: `prevPage()`, `nextPage()`, `clearFilters()` 后 SSE 仍使用旧参数
- **修复**: 操作后断开并重新初始化 SSE
- **代码变更**:
```javascript
// 修复前
function nextPage() {
    currentOffset += PAGE_SIZE;
    fetchPapers();
}

// 修复后
function nextPage() {
    currentOffset += PAGE_SIZE;
    disconnectSSE();
    fetchPapers();
    initSSE();
}
```
> 历史归档说明：此文件为历史报告文档，存在阶段性假设，仅作历史参考，不作为当前执行依据。
