# Lit AI Collector & Literature AI

项目包含两个子系统：

- **Lit AI Collector** — PySide6 桌面客户端，全球文献检索与 PDF 下载
- **Literature AI** — FastAPI 后端 + 静态前端，文献解析与 AI 写作辅助

---

## 子系统 A: Lit AI Collector (桌面客户端)

基于 PySide6 (Qt) 开发的高颜值桌面文献采集与下载工具，打通全球主流学术数据库及中文学术社区。

### 功能

- 高颜值 Neumorphism UI，支持高 DPI 缩放
- 全球多源联合检索：PubMed、Semantic Scholar、IEEE Xplore、Web of Science、arXiv、OpenAlex、X-MOL、Google Scholar
- 混合检索与 DOI/Title 跨源去重，交叉补全摘要和影响因子
- PaperFinder 20+ 并发 PDF 下载引擎（Sci-Hub、LibGen、AnnasArchive 等）
- 精确 DOI/ISBN/URL 下载，Dracula 暗黑风实时控制台终端
- 本地 SQLite 文献库与项目管理
- 生产级 PyInstaller 打包

### 开发运行

```bash
python launch.py           # 本地开发
pyinstaller LitAICollector.spec --clean --noconfirm  # 生产打包
```

### 核心结构

```text
app/
  ui/          # PySide6 GUI (main_window, search_page, library_page)
  services/    # findpapers 封装、X-MOL、Google Scholar、PDF 下载器
findpapers/    # 多数据库联合检索核心
src/           # PaperFinder 20+ 并发爬虫引擎
```

---

## 子系统 B: Literature AI (Web 后端 + 前端)

AI 辅助文献解析与写作支持，面向 DFT 研究、单/双原子催化剂、锂硫电池正极等领域。

### 进度：约 90%

### 已完成功能

**后端：**
- PDF 入库（路径/上传/DOI 下载）+ GROBID + Docling 双解析
- Stage 2 抽取（DFT/催化剂/电化学/机理/写作卡/综合分析 12 分类 + `type_confidence`）
- 文件夹即库（LibraryManager + 注册表 + 创建/激活/导入/移除）
- 参考文献管理（ReferenceEntry CRUD）+ 每篇文献持久化序号（serial_number）
- 在线文献检索（OpenAlex/arXiv）
- AI workflow 后台任务（非阻塞 + 轮询）
- RAG pipeline（检索 → 证据包 dedup + round-robin → 写作 → citation guard）
- Citation guard fact-level（mediates/infers_causality 触发词 + fact-claim repair）
- MCP 协作层（外部 AI 读论文/批注/提修正/触发解析）
- 外部 AI 导入（ExternalAnalysis import/materialize）
- 3 种 writer 后端：rule / llm_stub / openai_compatible（自动降级）
- 所有路由注册（papers/corrections/references/libraries/system/mcp/writer）

**前端：**
- 统一单页工作台（`literature_library/index.html`）：文献列表 + 序号 + 状态标签
- 5 标签视图：论文详情 / 内部 AI 整理 / 外部 AI 审核 / AI 检索入库 / 聚合视图
- 库管理工具栏（新建/导入/移除/下拉切换）
- AI workflow 后台任务轮询 UI
- 在线检索（100 条批次/去重）

### 变更记录

| 日期 | 变更 |
|------|------|
| 2026-05-23 | RAG Pipeline 硬化：evidence pack 全局去重 + round-robin；citation guard 扩展 mediates/infers_causality；fact-claim repair |
| 2026-05-23 | MCP 协作层 + 外部 AI 导入闭环 |
| 2026-05-23 | AI workflow 后台任务（非阻塞 + 轮询 UI） |
| 2026-05-22 | 文件夹即库：LibraryManager + 注册表 + 前端库管理 UI |
| 2026-05-19 | Writer prompt 精简 + citation guard 多轮扩展 + DeepSeek 烟雾测试通过 + 前端 guard 可视化 |
| ~2026-05 | RAG pipeline 基础（检索/压缩/3 后端/citation guard）+ discovery 适配层 + 关键词搜索 + 电化学/DFT 抽取器 |

### 待完成

1. **左右分栏拖拽** — git checkout 事故中丢失，需重建 `initSplitDrag`（仅 `index.html`）
2. 文件夹即库端到端测试（Docker 验证完整流程）
3. 真实 LLM 质量调优
4. 前端产品化 polish
5. 端到端集成测试

### Quick start

```bash
cd literature-ai
docker compose up --build
curl http://localhost:8000/api/health
```

### Key API endpoints

- `GET /api/papers` — 文献列表（?q=/?year=/?journal= 筛选）
- `GET /api/papers/discovery/search?q=...` — 在线检索
- `POST /api/papers/discovery/download` — DOI 下载
- `POST /api/papers/ingest/upload` — PDF 上传入库
- `GET /api/libraries` — 库列表
- `POST /api/libraries/{name}/activate` — 激活库
- `GET /api/writer/status` — 写作器状态
- `POST /api/writer/draft` — 生成草稿

### 协作规则

见 [literature-ai/AGENTS.md](./literature-ai/AGENTS.md) 和项目根 [AGENTS.md](./AGENTS.md)。

---

## 技术栈

| 组件 | 桌面客户端 | Web 后端 |
|------|-----------|---------|
| 框架 | PySide6 (Qt) | FastAPI |
| 数据库 | SQLite + SQLModel | SQLite（每库独立） |
| 检索引擎 | findpapers + PaperFinder | findpapers (适配层) |
| PDF 解析 | PyMuPDF (fitz) | GROBID + Docling |
| AI | openai API | openai API + 规则抽取器 |
| 打包 | PyInstaller | Docker Compose |
| 存储 | 本地文件系统 | 本地（每库 storage/） |
