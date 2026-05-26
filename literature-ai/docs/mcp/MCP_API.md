# Literature AI MCP API

## 服务地址

```text
http://localhost:8000/mcp
```

传输方式：

- Streamable HTTP

认证方式：

- `Authorization: Bearer <MCP_API_KEY>`

## 基本环境变量

```env
LITAI_MCP_ENABLED=true
LITAI_MCP_SERVER_NAME=Literature AI MCP
LITAI_MCP_API_KEYS=claude|Claude Desktop|litmcp_claude|read_papers,append_notes,propose_corrections,request_parse;admin|Admin|litmcp_admin|read_papers,append_notes,propose_corrections,request_parse,review_corrections
```

单条 key 格式：

```text
source_prefix|display_name|raw_api_key|capability1,capability2
```

## 支持的能力

- `read_papers`
- `append_notes`
- `propose_corrections`
- `request_parse`
- `review_corrections`

## 常见工具

贡献者常用：

- `query_papers`
- `get_paper`
- `list_notes`
- `append_note`
- `propose_correction`
- `parse_paper`
- `get_parse_status`
- `scan_local_pdfs`
- `ingest_pdf_batch`

审核者常用：

- `get_correction_queue`
- `get_correction_detail`
- `approve_correction`
- `reject_correction`

## 协作原则

- 外部 AI 可以读、记笔记、提修正建议、请求解析
- 外部 AI 不应被视为自动 verified 审核者
- 任何修正建议都应区分“候选提案”和“已人工确认”

## 推荐流程

1. `query_papers`
2. `get_paper`
3. `append_note` 记录软性问题
4. `propose_correction` 提交明确修改建议
5. 审核者使用 `get_correction_queue`
6. 审核者使用 `approve_correction` 或 `reject_correction`
