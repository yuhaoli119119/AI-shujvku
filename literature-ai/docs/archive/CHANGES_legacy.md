# CHANGES.md — 改动记录

> 本文件记录所有代码改动，供其他 AI 或协作者查看和查收。

---

## Phase 1: 后端基础设施升级 — Embedding + Writer 依赖

**日期**: 2026-05-24
**测试状态**: 149/149 通过（`test_papers_api.py` 因缺少 `requests` 包跳过，与本次改动无关）

### 1.1 Embedding 服务升级

**文件**: `backend/app/services/embedding.py`

**改动内容**:
- 新增 `EmbeddingService` Protocol — 定义所有 embedding 服务必须满足的接口
- 保留原 `DeterministicEmbeddingService` 不变（离线回退用）
- 新增 `OpenAICompatibleEmbeddingService` — 调用 OpenAI/DeepSeek 等 API 获取真实语义 embedding
  - API 调用失败时自动回退到 `DeterministicEmbeddingService`
  - 支持 `httpx` 同步调用
  - 自动构建 `/v1/embeddings` URL
- 新增 `get_embedding_service()` 工厂函数 — 根据 provider 配置返回对应服务
  - `provider="deterministic"` → DeterministicEmbeddingService（默认）
  - `provider="openai_compatible"` → OpenAICompatibleEmbeddingService

**新增依赖**: 无（`httpx` 已在项目中使用）

---

### 1.2 Retriever 适配新 Embedding

**文件**: `backend/app/rag/retriever.py`

**改动内容**:
- import 行新增 `get_embedding_service`, `EmbeddingService`
- `Retriever.__init__()` 签名新增 `embedding: EmbeddingService | None = None` 参数
  - 传入 embedding 时使用传入的服务
  - 不传时保持原有 `DeterministicEmbeddingService` 行为（向后兼容）

**影响范围**: 仅初始化逻辑，检索逻辑完全不变

---

### 1.3 Writer 强制依赖 LLM 配置

**文件**: `backend/app/rag/backends.py`

**改动内容**:
- `OpenAICompatibleWriterBackend.generate()` 中:
  - **missing_config 时**: 不再静默回退到 `RuleWriterBackend`，改为返回空 sections + 明确错误提示
    - `llm_error` 现在是中文: "请先在设置页面配置 API：缺少 xxx。Writer 必须配置 LLM 才能生成内容。"
    - `backend_used` 直接是 `openai_compatible`（不再带 `->rule` 后缀）
  - **LLM 调用失败时**: 不再静默回退到 rule 模板，改为返回空 sections + 错误信息
    - `llm_error` 改为中文提示

**影响范围**: 
- 使用 `openai_compatible` backend 且未配置 API 时，`write()` 返回的 `sections` 为空字典
- 前端需要检查 `llm_status` 和 `llm_error` 来展示配置提示

---

### 1.4 配置新增字段

**文件**: `backend/app/config.py`

**改动内容**:
- 新增 4 个 embedding 相关配置字段:
  - `embedding_provider: str = "deterministic"` — embedding 服务提供者
  - `embedding_api_base: str | None = None` — API base URL
  - `embedding_api_key: str | None = None` — API Key
  - `embedding_model: str = "text-embedding-3-small"` — 模型名
- 保留原有 `embedding_dimension: int = 64`

**环境变量**: 对应 `LITAI_EMBEDDING_PROVIDER`, `LITAI_EMBEDDING_API_BASE`, `LITAI_EMBEDDING_API_KEY`, `LITAI_EMBEDDING_MODEL`

---

### 测试修复

**文件**: `backend/tests/test_rag_workflow.py`
- `test_retriever_writer_and_citation_guard_work_together` — 断言从 `startswith("openai_compatible->")` 改为 `== "openai_compatible"`

**文件**: `backend/tests/test_writer_fact_repair.py`
- 新增 `_make_mock_writer()` 辅助函数 — 解决 `MagicMock(spec=Writer)` 拦截类属性（`SENTENCE_SPLIT_PATTERN`/`TOKEN_PATTERN`）的问题
- 所有测试方法改用 `_make_mock_writer(guard)` 代替手动构建 MagicMock

---

## Phase 2: 设置页面 + API 配置

**日期**: 2026-05-24
**测试状态**: 149/149 通过

### 2.1 后端 Settings API

**文件**: `backend/app/api/settings.py`（新建）

**改动内容**:
- 新增 `app_settings` 数据库表 — 存储用户配置（key-value 对），持久化在 SQLite 中
- `GET /api/settings` — 返回当前配置，敏感值（含 key/secret/token）自动遮罩
- `POST /api/settings` — 更新配置，仅接受白名单内的 key，遮罩值自动跳过
- `GET /api/settings/status` — 返回各服务（Embedding / Writer / MCP）的连接状态
- `GET /api/settings/ide-prompts` — 自动生成 IDE 连接配置（MCP JSON + 一键提示词），根据本机 IP 动态生成 URL
- `_get_active_engine()` — 优先使用 `session._engines` 中活跃的引擎，回退到配置默认值
- `_apply_settings_to_runtime()` — 更新运行时 Settings 实例 + 环境变量

**设计决策**:
- API Key 存 SQLite（用户永远不需要手动编辑 .env）
- 通过 `_MANAGED_KEYS` 白名单控制可修改的配置项
- 遮罩值（含 `****`）在保存时自动跳过，避免覆盖真实密钥

---

### 2.2 主路由注册

**文件**: `backend/app/main.py`

**改动内容**:
- 新增 `from app.api.settings import router as settings_router`
- 新增 `app.include_router(settings_router, prefix="/api/settings", tags=["settings"])`

**影响范围**: 1 行 import + 1 行 include_router

---

### 2.3 前端设置页面

**文件**: `frontend/pages/settings/index.html`（新建）

**改动内容**:
- 完整设置页面，暗色主题设计（CSS 变量驱动，支持实时切换暗色/亮色/护眼）
- 四个分区导航：
  1. **API 配置** — Embedding 模型 + Writer LLM + MCP Keys 的配置表单
  2. **IDE 连接** — 自动生成的 MCP JSON 配置 + 一键提示词 + 连接信息
  3. **主题外观** — 亮/暗/护眼切换 + 6 种设计风格预告
  4. **使用说明** — 从 `使用说明.md` 搬运的完整指南
- 顶部导航栏：文献库 / 论文详情 / AI Writer / 外部审稿 / 机理知识 / DFT 数据库 / 写作卡片 / 设置
- 服务状态实时展示（Embedding / Writer / MCP 各自的配置状态）
- Toast 通知系统
- 主题选择持久化到 localStorage

---

## Phase 3: 前端 UI 重设计

> 待实施（设计系统 + 导航组件 + 页面重构）

---

## Phase 4: MCP 4 个新工具

**日期**: 2026-05-24
**测试状态**: 159/159 通过

### 4.1 共享函数提取

**文件**: `backend/app/services/external_analysis_service.py`

**改动内容**:
- 新增 `_truncate()` — 文本截断辅助函数
- 新增 `build_internal_ai_review_blob()` — 构建 AI 审阅用的论文数据 JSON bundle
- 新增 `sanitize_internal_corrections()` — 清洗修正提案的 target_path

**文件**: `backend/app/api/external_analysis.py`

**改动内容**:
- 删除内联的 `_truncate` / `_build_internal_ai_review_blob` / `_sanitize_internal_corrections`
- 改为从 `external_analysis_service.py` import 共享版本
- 移除不再需要的 `import json`

---

### 4.2 MCP 新增 4 个工具

**文件**: `backend/app/mcp/server.py`

**新增工具**:
- `retrieve_evidence` — 语义检索跨论文结构化证据（DFT/机理/电化学/写作卡片/图片数值），权限: `read_papers`
- `review_paper` — 触发 AI 深度审阅，生成结构化修正提案，支持 `auto_apply`，权限: `propose_corrections`
- `import_analysis` — 导入外部 AI 分析结果（自由文本或结构化 JSON），自动归一化，权限: `propose_corrections`
- `compare_papers` — 多论文结构化数据对比（2-10篇），权限: `read_papers`

**新增 import**: `Retriever`, `ExternalAnalysisService`, `ExternalAnalysisNormalizedModel`, `build_internal_ai_review_blob`, `sanitize_internal_corrections`, `run_in_threadpool`

---

### 4.3 测试

**文件**: `backend/tests/test_mcp_new_tools.py`（新建）

**测试用例**: 9 个 — retrieve_evidence 成功/过滤、review_paper 成功/LLM未配置、import_analysis 成功/仅文本、compare_papers 成功/字段过滤/无效数量

---

## Phase 5: 全量验证

**日期**: 2026-05-24
**测试状态**: 159/159 通过

- 全量 pytest 回归测试通过（跳过因缺少 `requests` 的 `test_papers_api.py`）
- 文件夹库 CRUD 验证通过（新建/激活/切换/导入/移除/默认库保护）
- 前端分栏拖拽增强（鼠标+触摸+RAF节流+localStorage持久化）
> 历史归档说明：此文件为旧版变更记录，存在编码损坏和阶段性描述，仅作历史参考，不作为当前执行依据。当前真实进度以根 README、`literature-ai/AGENTS.md` 与 git history 为准。
