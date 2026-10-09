# MCP 外部接入现状

> 本文只记录**当前可用**的接入事实（2026-09-22 核对）。MCP 工具面本身属于旧流程的一部分，
> 目标流程落地后会收敛，届时另行更新。

## 端点与鉴权

- 端点：`https://dft.researchlife.top/mcp`（FastMCP，Streamable HTTP）
- 鉴权头：`Authorization: Bearer <MCP_KEY>`
- 不带 key → `401`（网关对 `/mcp` 关闭了会话校验，交给后端用 Bearer 鉴权）
- key 与能力清单存在服务器 `/opt/literature-ai/.env` 的 `LITAI_MCP_API_KEYS`
  （格式 `来源|显示名|key|能力`）。
- **key 绝不写进任何会外传的文档、脚本、截图或对话**；泄露即在 `.env` 删除/更换该条并
  `docker compose up -d --no-deps --force-recreate backend` 吊销。

外部 AI 的配置示例（key 用实际值替换）：

```json
{
  "mcpServers": {
    "literature-ai": {
      "url": "https://dft.researchlife.top/mcp",
      "headers": { "Authorization": "Bearer <MCP_KEY>" }
    }
  }
}
```

## 权限分层（不要越层给权限）

| 层级 | 谁 | 能做什么 | 接入方式与凭据 |
|---|---|---|---|
| L1 代码/文档 | 任何 AI | 读改代码、读文档 | 服务器上的仓库目录（不经 MCP） |
| L2 数据查询 | 外部 AI / IDE | 只读 + 受控写入文献数据 | MCP 端点 + 仅 MCP key（可吊销、能力受限） |
| L3 服务器运维 | 服务器上的会话（如 Codex-web） | docker、备份、改 `.env`、重启 | SSH / 服务器本地权限；**绝不给外部 AI** |

## 与目标流程的关系

- 检索、读页、读图这类**只读**能力在目标流程里仍然需要；
- 旧流程里围绕"审核对象、批量验收、多层门禁"的写入口，属于被重建替换的部分，
  不要把它们的调用方式继续当作新流程的规范；
- 目标流程的数据规则见 [`DATA_RULES.md`](DATA_RULES.md)。
