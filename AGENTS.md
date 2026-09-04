# AI-shujvku — 项目与 Agent 协作指南

本仓库为**本地文献 AI 系统**（literature-ai，PostgreSQL+pgvector 为数据唯一真源），生产环境已部署到本地服务器。任何 AI（Codex / 豆包 / 其他 agent）在此仓库执行任务前，请先读本节。

## 接手第一步（任何 AI 先做这个）

1. 通读本文件；涉及服务器登录/备份的细节再读 `local/srv_deploy/README.md`（该目录 gitignore，含凭据，不外传）。
2. 先判断任务类型：**改数据/迁移/清理 → 先备份**（见下）；**改代码 → 走代码同步链路**；**只读查询 → 不动数据**。
3. 数据真源只有一个：服务器 PostgreSQL `literature_ai` 库；本机文件、向量、PDF 都是派生，禁止拿派生覆盖真源。
4. 远程操作统一走 `local/srv_deploy/sshkit.py`，复杂 bash 写成 `.sh` 上传执行（见文末工程注意）。
5. 动手前如发现现状与本文不符，**以服务器实际状态为准并回头修订本文**，不要凭文档臆测。

## 服务器部署（生产环境）

- 主机：**192.168.110.229**（Rocky Linux 9.4，Docker Compose 部署 9 个服务）
- 部署路径：`/opt/literature-ai`；数据目录 `/opt/literature-ai/data/`
- SSH 凭据、一键备份/恢复工具、运维命令：**见 `local/srv_deploy/README.md`**（该目录已被 gitignore，含 `cred.env` 凭据与 `backup_db.py`）

## 数据库备份（其他 AI 必须知晓）

- **权威备份位置**：
  - 服务器：`/home/2401liyuhao/literature_ai_latest.dump`（pg_dump -Fc）
  - 本机同步副本：`local/backups/runtime/literature_ai_latest.dump`
- **一键备份/恢复**（在 `local/srv_deploy/` 下执行）：
  ```powershell
  python backup_db.py backup          # 备份：服务器导出 → 同步到本机
  python backup_db.py restore --yes   # 恢复：本机 dump → 覆盖进服务器（危险）
  ```
- 涉及数据库的修改/迁移/清理任务，**先执行 `backup_db.py backup` 再动手**；任务结束后若改动过数据，再次备份。

## 数据组成（迁移/同步时需一起考虑）

| 内容 | 位置 | 说明 |
|---|---|---|
| 数据库（真源） | PostgreSQL `literature_ai` 库 | 99 篇文献、4 个文献库（激活：锂硫双原子） |
| PDF 原文 | `/opt/literature-ai/data/storage` | 约 1.6G |
| 文献库配置 | `/opt/literature-ai/data/libraries` + `library_registry.json` | 4 个文献库元数据 |
| docling 解析模型 | `/opt/literature-ai/data/docling_cache` | 离线解析 PDF 必需，约 506M |

## 代码同步（改代码 → 服务器实时更新）

- 代码仓库：GitHub `https://github.com/yuhaoli119119/AI-shujvku.git`，部署分支 `codex/content-knowledge-workbench-20260716`
- 更新链路：本机改代码 → 推送到 GitHub → 服务器 `/opt/ai-shujvku-src` 执行 `./update.sh`（git pull → 同步到 `/opt/literature-ai`，保护 `.env` 与 `data/` → 重建容器）
- **本机 git 注意**：当前 Windows 环境对 agent 会话的 git 写对象有沙箱拦截（`git add` 报 `Permission denied`），**commit/push 须由用户在普通终端（非 agent 会话）手动执行**；服务器侧 git 不受影响
- 服务器更新脚本：`/opt/ai-shujvku-src/update.sh`；手动执行 `cd /opt/ai-shujvku-src && ./update.sh`

## 对外访问与 MCP 接入（外部 AI 查询用）

- 生产网站（工作台）：`https://dft.researchlife.top`，经 cloudflared 隧道 → 服务器本机 8000 Owner 网关。
- MCP 端点：`https://dft.researchlife.top/mcp`（FastMCP，Streamable HTTP），鉴权头 `Authorization: Bearer <MCP_KEY>`；不带 key 返回 401。
- MCP key 与能力清单存在服务器 `/opt/literature-ai/.env` 的 `LITAI_MCP_API_KEYS`（格式 `来源|显示名|key|能力`），本机开发用副本在根目录 `.mcp.json`（已 gitignore）。**key 不写进任何会 push 的文档。**
- 当前 key 名 `local_ide`，能力：read_papers / append_notes / propose_corrections / request_parse / review_corrections / review_dft / create_share_links / ai_verify_content。
- 外部 AI 的 MCP 配置示例（key 用实际值替换）：
  ```json
  { "mcpServers": { "literature-ai": {
      "url": "https://dft.researchlife.top/mcp",
      "headers": { "Authorization": "Bearer <MCP_KEY>" } } } }
  ```

## 权限分层与凭据规则（决定给其他 AI 什么权限）

按"能接触到什么"把协作者分三层，**不要越层给权限**：

| 层级 | 谁 | 能做什么 | 用什么接入 | 凭据 |
|---|---|---|---|---|
| L1 代码/文档 | 任何 AI（含云端） | 读改代码、读文档、提 PR | GitHub 仓库 | 无需密码 |
| L2 数据查询 | 外部 AI / IDE | 只读+受控写入文献数据 | MCP 端点 | 仅给 MCP key（可吊销、能力受限） |
| L3 服务器运维 | **仅本机 AI** | docker、备份、改 .env、重启 | SSH + sshkit.py | root 密码只在本机 `local/srv_deploy/cred.env` |

铁律：
1. **服务器 root 密码永远不进 GitHub、不发给云端/网页版 AI、不贴进公开对话**；它只存在本机 `cred.env`（gitignore）。
2. 本机 AI 要运维服务器时，自己读 `cred.env` / 用 `sshkit.py`，**不需要用户在对话里重复发密码**。
3. 外部 AI 只需要查数据就给 **MCP key**（L2），绝不给 SSH（L3）；需要改代码就走 GitHub（L1），由本机 AI 或用户在服务器跑 `update.sh` 落地。
4. MCP key 泄露可在 `.env` 的 `LITAI_MCP_API_KEYS` 里删除/更换后 `--force-recreate` backend 即吊销。
5. 一句话交接模板（对任何新 AI）：**"先读仓库根 AGENTS.md；要查数据走 MCP；要动服务器先读 local/srv_deploy/README.md 并先备份。"**

## 日常运维速查（本机，在 local/srv_deploy/ 下）

```powershell
# 凭据（每个新终端先设，或 Get-Content cred.env）
$env:SRV_HOST='192.168.110.229'; $env:SRV_USER='root'; $env:SRV_PWD='见cred.env'
python sshkit.py run "cd /opt/literature-ai && docker compose ps" 30   # 看容器状态
python backup_db.py backup                                              # 备份数据库（改数据前后）
```
服务器侧常用（root）：`docker compose logs -f backend`、`docker compose restart backend worker worker-pdf`、
`docker exec -it literature-ai-postgres-1 psql -U literature_ai -d literature_ai`。
健康检查：`curl https://dft.researchlife.top/api/health`。

## 网关安全加固（2026-09-04 已实施）

Owner 网关（`deploy/nginx/owner.conf.template`，经 cloudflared 暴露公网）已加一层 **HTTP Basic 登录门**，外部黑盒+服务器内测 13 项全部通过：

- **匿名访问**：页面与所有 `/api/*`（content-knowledge / papers / settings 等）一律 **401**；`/docs`、`/redoc`、`/openapi.json` 一律 **404**。
- **`/mcp` 豁免 Basic**：`auth_basic off`，继续由后端用 `Authorization: Bearer <MCP key>` 鉴权（无 key 401、带 key 307 握手），外部 AI 接入方式不变。
- **`/api/health` 放行**（隧道/监控探测，仅暴露库名）。
- **带正确 Basic**：页面与 API 正常 200，工作台前端无需改动（浏览器首次输入后同源请求自动带凭据）。
- Basic 凭据文件：服务器 `literature-ai/deploy/nginx/owner.htpasswd`（挂载到容器 `/etc/nginx/.htpasswd`），**已 gitignore、永不入库**；明文用户名/密码只记在本机 `local/srv_deploy/README.md`。
- 改 Basic 密码：服务器上 `docker exec literature-ai-owner-gateway-1 sh -c "printf 'owner:%s\n' \"\$(openssl passwd -apr1 '新密码')\" > /etc/nginx/..."`（或改宿主机 `deploy/nginx/owner.htpasswd` 后 `docker compose up -d --no-deps --force-recreate owner-gateway`）。
- `update.sh` 的 rsync 已 `--exclude '*.htpasswd'`，代码更新不会删凭据；`docker-compose.yml` 的 owner-gateway 已挂载 htpasswd，**改这两个文件必须同步回仓库，否则下次更新会丢挂载**。
- 回滚：`/root/gateway_bak_*/` 有加固前的 owner.conf.template 与 docker-compose.yml 备份。
- 可选进阶（未做）：Cloudflare Zero Trust Access 再加一层零信任登录；share-gateway 仍在局域网 `0.0.0.0:8080`（设计为只读白名单，风险低）。

## 工程注意

- Windows 本地执行远程命令时，PowerShell 会破坏 `/dev/null`、`$()`、`*` 等 → 远程 bash 逻辑写成 `.sh` 上传执行。
- 服务器不可达 HuggingFace / Docker Hub；docling 模型已本地缓存，解析新 PDF 无需外网。
- 本机 agent 会话 git 写对象被沙箱拦截，勿在 agent 会话内尝试 `git add`/`git commit`/`git push`（会报 Permission denied），统一走用户普通终端。
