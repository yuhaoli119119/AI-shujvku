# Literature AI MCP 实施方案

> 版本：v1.1  
> 日期：2026-05-20  
> 目标：把 `literature-ai` 暴露为可被外部 AI 调用、核对、批注、提议修订、触发解析的 MCP Server

---

## 1. 目标与边界

### 1.1 核心目标

本项目不是把外部 AI 直接变成数据库管理员，而是把 `literature-ai` 做成一个**可核对、可协作、可追溯**的文献解析工具：

1. 外部 AI 可读取已解析文献及结构化结果
2. 外部 AI 可对结果添加批注，指出疑点与补充信息
3. 外部 AI 可提交“待审核修订建议”，但**不能直接覆盖主数据**
4. 外部 AI 可触发 DOI / arXiv 文献解析
5. 所有操作可追踪来源并保留审计记录

### 1.2 设计原则

- **原始解析结果不直接覆盖**
- **写操作默认进入待审队列**
- **批注必须允许多 AI 共享**
- **接口优先复用现有后端能力**
- **MVP 先做最小闭环，再扩展管理员审核流**

### 1.3 第一阶段范围（本次实施）

本次先实现以下最小闭环：

- MCP Streamable HTTP 入口 `/mcp`
- API Key 鉴权
- 只读工具：
  - `query_papers`
  - `get_paper`
  - `list_notes`
  - `get_parse_status`
- 写工具：
  - `append_note`
  - `propose_correction`
  - `parse_paper`
- 数据表：
  - `paper_notes`
  - `paper_corrections`
  - `parse_jobs`
  - `audit_logs`

**暂不实现：**

- 自动审核通过
- 管理端 UI
- 直接修改主数据
- 复杂事实级语义 guard 联动
- 细粒度字段版本冲突处理

---

## 2. 数据模型

### 2.1 `paper_notes`

用途：保存外部 AI 或人工对论文的批注，不改主数据。

关键字段：

- `paper_id`
- `source`
- `content`
- `field_name`
- `page`
- `section_title`
- `quoted_text`
- `created_at`

说明：

- `source` 标识来源，如 `claude`、`gemini`、`cursor`
- `quoted_text` 用于保留外部 AI 认为存在问题的原文片段

### 2.2 `paper_corrections`

用途：保存“待审修订建议”。

关键字段：

- `paper_id`
- `source`
- `field_name`
- `target_path`
- `operation`
- `proposed_value`
- `reason`
- `evidence_payload`
- `status`
- `created_at`
- `reviewed_at`
- `reviewed_by`

说明：

- `operation` 先支持 `replace | add | delete`
- `target_path` 是逻辑路径，如 `abstract`、`dft_results_items[0].value`
- `proposed_value` 存 JSON
- `evidence_payload` 存页码、section、摘录等证据信息
- 第一阶段只写入队列，不自动应用

### 2.3 `parse_jobs`

用途：记录外部 AI 触发的解析任务。

关键字段：

- `identifier`
- `providers`
- `requested_by`
- `status`
- `paper_id`
- `error_message`
- `created_at`
- `updated_at`

说明：

- 当前实现可以是同步执行，但仍保留任务记录
- 后续可平滑切到异步队列

### 2.4 `audit_logs`

用途：追踪所有 MCP 写操作。

关键字段：

- `paper_id`
- `action`
- `source`
- `target_type`
- `target_id`
- `payload`
- `created_at`

---

## 3. 鉴权与权限模型

### 3.1 API Key

第一阶段不单独做 `api_keys` 表，改用环境变量配置，降低实现复杂度。

建议格式：

```env
LITAI_MCP_API_KEYS=claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections
```

每项格式：

```text
source_prefix|display_name|raw_api_key|capability1,capability2
```

### 3.2 能力粒度

第一阶段支持以下 capability：

- `read_papers`
- `append_notes`
- `propose_corrections`
- `request_parse`
- `review_corrections`

### 3.3 鉴权策略

- 所有 `/mcp` 请求都必须带 `Authorization: Bearer <key>`
- 未通过鉴权返回 `401`
- 权限不足返回 `403`
- 当前请求的认证结果写入上下文，供工具函数记录 `source`

---

## 4. MCP Tool 设计

### 4.1 `query_papers`

用途：检索已解析文献列表。

输入：

- `q`
- `year`
- `journal`
- `has_dft_results`
- `has_writing_cards`
- `limit`
- `offset`

输出：

- 文献列表
- 每篇文献的计数信息

### 4.2 `get_paper`

用途：读取单篇论文完整详情。

输入：

- `paper_id`

输出：

- 论文元信息
- sections / figures / tables
- dft / electrochem / mechanism / writing card 结构化结果

### 4.3 `list_notes`

用途：查看某篇论文现有批注。

输入：

- `paper_id`
- `source`（可选）

输出：

- 批注列表

### 4.4 `append_note`

用途：给论文追加批注。

输入：

- `paper_id`
- `content`
- `field_name`（可选）
- `page`（可选）
- `section_title`（可选）
- `quoted_text`（可选）

输出：

- 新建批注记录

### 4.5 `propose_correction`

用途：提交待审核修订建议。

输入：

- `paper_id`
- `field_name`
- `target_path`
- `operation`
- `proposed_value`
- `reason`
- `evidence_payload`（可选）

输出：

- 新建修订建议记录
- 当前状态 `pending`

### 4.6 `parse_paper`

用途：按 DOI / arXiv 标识符触发解析。

输入：

- `identifier`
- `providers`（可选）

输出：

- `job_id`
- `status`
- `paper_id`（如成功）

### 4.7 `get_parse_status`

用途：查询解析任务状态。

输入：

- `job_id`

输出：

- `status`
- `paper_id`
- `error_message`

---

## 5. 与现有代码的集成策略

### 5.1 复用已有服务

- `PaperQueryService`：用于 `query_papers` / `get_paper`
- `DiscoveryService`：用于 DOI / arXiv 元数据与 PDF 下载
- `PaperIngestionService`：用于实际解析与入库

### 5.2 新增模块

建议新增：

```text
backend/app/mcp/
  __init__.py
  auth.py
  server.py
  context.py
```

以及新增 schema：

```text
backend/app/schemas/mcp.py
```

### 5.3 FastAPI 集成方式

使用官方 MCP Python SDK 的 `FastMCP`，通过 Streamable HTTP 挂载到现有 FastAPI：

- 路径：`/mcp`
- 传输：`streamable-http`
- 返回：JSON 模式

---

## 6. 实施步骤

### Phase 1：最小 MCP 协作闭环

1. 修正文档并冻结接口命名
2. 扩展 `config.py` 增加 MCP 配置
3. 扩展 `models.py` 增加四张协作表
4. 扩展初始化 SQL
5. 新增 `schemas/mcp.py`
6. 新增 MCP 鉴权与上下文模块
7. 新增 MCP Server 与工具实现
8. 挂载到 `main.py`
9. 补充后端测试

### Phase 2：管理员审核流

后续再做：

- `approve_correction`
- `reject_correction`
- HTTP 管理接口
- 管理端 UI

### Phase 3：更强的证据与版本控制

后续可扩展：

- correction 绑定 `original_value_hash`
- 字段级版本冲突检测
- 更细粒度 evidence span 定位

---

## 7. 验收标准

### 功能验收

1. 外部 AI 能通过 MCP 列出论文
2. 外部 AI 能读取论文详情
3. 外部 AI 能查看同一论文的共享批注
4. 外部 AI 能添加批注
5. 外部 AI 能提交修订建议，且不会直接改主数据
6. 外部 AI 能触发 DOI / arXiv 解析并查询任务状态
7. 写操作会记录审计日志

### 安全验收

1. 未带 API Key 的 `/mcp` 请求返回 `401`
2. 权限不足的工具调用返回 `403`
3. 不同来源写入的 note / correction 能保留 `source`

### 代码验收

1. 不破坏现有 `/api/papers`、`/api/writer` 逻辑
2. 新增测试通过
3. README 进度记录同步更新

---

## 8. 本次实施决策

这次实现按以下决策落地：

- 使用官方 `mcp` Python SDK
- 使用 Streamable HTTP，而不是旧式 SSE-only MCP
- 第一阶段只允许“提议修订”，不允许直接应用修订
- API Key 先走环境变量，不先引入独立密钥管理表
- 解析任务先同步执行，但写入 `parse_jobs`，为后续异步化预留接口

---

## 9. 后续可选增强

- `get_paper_evidence`：按字段拉取 evidence spans
- `reparse_paper`：重跑已存在论文的解析
- `diff_corrections`：比较多个修订建议
- 管理员审核 MCP 工具
- 把 correction 应用到独立 curated 层，而非直接写主表

---

## 10. 结论

这个方案的关键，不是让更多 AI 直接“改库”，而是让更多 AI 参与**核对、批注、提议、复核**。  
第一阶段先完成最小闭环，保证：

- 能读
- 能批注
- 能提议修订
- 能触发解析
- 全过程可追踪

在这个基础上，再做审核流和更强版本控制，会更稳。
