# Literature AI

Literature AI 是一款先进的文献解析与 AI 写作辅助系统，专为 **DFT 催化研究、单/双原子催化剂、锂硫电池正极** 等材料与化学领域的科研人员打造。系统通过打通多源学术数据库、自动解析学术文献、抽取结构化知识，结合高保真的 RAG（检索增强生成）与 MCP 协作层，为学术阅读、知识归纳与论文写作提供全方位的智能辅助。

---

## 🌟 核心系统特性

### 1. FastAPI 异步后端
- **高性能 Web 核心**：基于 FastAPI 开发，利用 Python 异步生态实现高并发的 API 响应。
- **完善的 API 路由**：模块化设计，提供包括文献（papers）、库管理（libraries）、参考引用（references）、系统状态（system）、写作编排（writer）及 MCP 服务等在内的全套 API 路由。

### 2. 单页模块化静态前端
- **集成化工作台**：位于 `literature-ai/frontend/pages/` 下，核心主工作台入口为 `http://localhost:8000/pages/literature_library/index.html`。
- **多功能标签视图**：包含论文详情、内部 AI 整理、外部 AI 审核、在线检索入库以及数据聚合等多标签视窗，支持实时的状态更新和任务通知。
- **极简主题切换**：支持暗色/亮色/护眼主题的外观切换，配置实时保存，界面优雅现代。

### 3. 双通道 PDF 解析与多源入库
- **智能双解析引擎**：结合 **GROBID** 与 **Docling** 解析器。提供高精度的段落、数学公式、表格以及文献引用的细粒度结构化提取。
- **全自动 PDF 入库**：
  - 支持本地 PDF 批量上传与导入。
  - 支持扫描服务器/宿主机指定磁盘路径直接解析入库。
  - 支持输入 DOI，通过内置多源检索自动下载、抓取 PDF 并入库。

### 4. 文件夹即库 (LibraryManager)
- **零配置多文献库管理**：采用“文件夹即库”设计，自动为每个文献库建立独立的 SQLite 数据表与文件存储空间。
- **便捷切换**：前端提供库切换下拉菜单，支持新建库、导入现有目录、库激活以及一键移除等功能。

### 5. 异步 AI Workflow 任务流
- **非阻塞后台任务**：结合 Celery 与 Redis，文献的 PDF 下载、双通道解析、Stage 2 结构化数据抽取（DFT/催化剂/电化学/机理等 12 维度特征）均在后台异步执行。
- **前端实时轮询**：前端可动态获取后台解析进度与状态，防止大文件解析阻塞用户当前的交互操作。

### 6. 高保真 RAG 流程 (RAG Pipeline)
- **智能证据包编排**：混合词法与语义向量检索，对检索到的证据段落（Evidence Pack）进行全局去重与 Round-Robin 分段排序。
- **多写作后端支持**：内置 `rule`、`llm_stub`、`openai_compatible` 三种写作生成后端，支持自动故障回退与服务降级。
- **事实级别 Citation Guard**：内置数值与逻辑事实校验，通过识别推理触发词，智能检测 RAG 生成段落中的事实偏差，自动发起 Fact-Claim 修正提案。

### 7. MCP (Model Context Protocol) 协作层
- **对外 AI 接口标准**：全面兼容 Anthropic 倡导的 MCP 规范。
- **强大的协作工具箱**：暴露 `retrieve_evidence`、`review_paper`、`import_analysis`、`compare_papers` 等 4 个核心 MCP 工具，使外部 AI（如 Cursor/Claude 等 IDE 辅助工具）可以无缝读取文献库、提交修正提案、导入外部 AI 分析，甚至触发文献的重新解析。

---

## 🛠 技术栈

| 组件 | Web 系统技术栈 |
|------|--------------|
| **Web 核心** | FastAPI (Python 3.11-slim) |
| **异步任务** | Celery + Redis |
| **数据库** | PostgreSQL/pgvector (主向量数据库) + SQLite (单文献库元数据) |
| **学术检索引擎**| findpapers (内置子包) + OpenAlex API + arXiv API |
| **PDF 解析引擎**| GROBID + Docling |
| **部署与编排** | Docker Compose 一键虚拟化 |
| **本地文件存储**| 本地磁盘映射挂载 + MinIO 对象存储 |

---

## 🚀 快速开始 (Docker Compose 启动)

请确保您的系统已安装了 [Docker](https://www.docker.com/) 和 Docker Compose。

### 1. 运行一键构建并启动
```bash
cd literature-ai
docker compose up --build
```
该命令会自动下载并运行以下核心容器：
- `postgres` (带 pgvector 插件)
- `redis` (Celery 队列中介)
- `minio` (PDF 及 TEI 对象存储)
- `grobid` (PDF 结构分析器)
- `backend` (FastAPI 异步主服务)
- `worker` (Celery 后台异步任务处理器)

### 2. 验证系统健康状态
在命令行或浏览器中访问：
```bash
curl http://localhost:8000/api/health
```
如果返回正常的健康状态 json，代表服务启动成功。

### 3. 访问前端页面
直接用浏览器打开前端静态网页即可开始工作：
- [文献库主工作台](http://localhost:8000/pages/literature_library/index.html)

---

## 📅 近期变更历史 (RAG & Web 专版)

| 日期 | 变更内容 |
|------|------|
| 2026-05-24 | **网页端与桌面端彻底解耦**。内置原外部 `findpapers` 包，移除 docker-compose 中对宿主机外部 app 目录卷挂载依赖，实现网页端 100% 独立开发与打包。 |
| 2026-05-23 | RAG Pipeline 硬化：证据包全局去重 + Round-Robin 排序；事实级别 Citation Guard 扩展 mediates/infers_causality 以及 Fact-Claim 校验。 |
| 2026-05-23 | MCP 协作层服务硬化与外部 AI 提案导入全闭环。 |
| 2026-05-23 | AI Workflow 后台解析任务（非阻塞 + 轮询前端 UI）上线。 |
| 2026-05-22 | 文件夹即库：设计 LibraryManager + 数据库轻量注册表，开发前端库管理交互 UI。 |
| 2026-05-19 | AI Writer 提示词精简 + 引用守护组件多轮扩展 + 前端 Guard 结果可视化。 |

---

## 📁 项目目录结构

```text
AI-shujvku/
  ├─ literature-ai/             # Web 系统的核心目录
  │    ├─ backend/              # FastAPI 异步后端应用
  │    │    ├─ app/             # 业务接口与逻辑层 (api, db, rag, schemas, workers)
  │    │    ├─ findpapers/      # 内置学术文献多源联合检索子模块
  │    │    └─ tests/           # 单元测试与集成测试用例
  │    ├─ frontend/             # 静态前端模块
  │    │    └─ pages/           # 前端业务视窗 (literature_library, settings 等)
  │    ├─ prompts/              # LLM 的 RAG 提示词配置文件
  │    ├─ storage/              # PDF、TEI 解析产物本地缓存目录
  │    ├─ docker-compose.yml    # 容器编排配置文件
  │    └─ AGENTS.md             # 专属于网页端的 AI 协作同步规范
  ├─ scripts/                   # 网页端自动化接口流水线实用脚本
  ├─ CHANGES.md                 # 项目更新说明文档
  └─ README.md                  # 本说明文件
```

---

## 🤝 协作规则

请所有参与修改和协作的 AI 或开发人员在开始编码前严格遵守：
- [literature-ai/AGENTS.md](./literature-ai/AGENTS.md)（专属于网页端系统的具体规范）
