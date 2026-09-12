# AI-shujvku — 项目与 Agent 协作指南

本仓库为**本地文献 AI 系统**（literature-ai，PostgreSQL+pgvector 为数据唯一真源），生产环境已部署到本地服务器。任何 AI（Codex / 豆包 / 其他 agent）在此仓库执行任务前，请先读本节。

## 顶级规则：一切以服务器运行态和用户实际看到的结果为准

本规则优先于本文件内其他开发、测试、交付和验收说明：

1. **服务器运行态是唯一交付真源。** 真正工作区为服务器 `/opt/literature-ai`；本地仓库、工作树、测试环境、测试报告、Git 状态和本地文件哈希均不能代表服务器已经更新或用户已经看到正确结果。
2. **用户实际看到的页面是前端验收的最高标准。** 只要用户浏览器中显示的页面、文案、结构、交互或数据不符合要求，就必须判定任务未完成；不得用“本地已修改”“测试已通过”“服务器文件哈希一致”“接口返回 200”反驳用户看到的事实。
3. 涉及页面修改、回滚或发布时，必须在服务器实际运行环境完成核对，并通过用户正在访问的真实入口验证最终页面。未完成服务器验证和真实页面验证时，只能报告“本地完成，服务器/用户侧尚未验收”，禁止声称“已完成”“已闭环”或“已交付”。
4. 本地代码与服务器不一致时，必须明确报告差异，并以服务器现状为准继续判断；不得基于本地版本替服务器运行态作结论。
5. 用户明确表示看到的结果不正确或不满意时，应立即停止围绕本地测试继续辩解，先核查服务器运行态、实际响应、浏览器缓存与用户入口，直到用户能够看到正确结果。

## 当前最高优先级：先跑通一篇论文

当前项目的唯一核心目标是先跑通一篇论文的数据提取闭环：

PDF 解析
→ 图表解析与审核
→ DFT 数据提取
→ DFT 证据核验
→ 单篇内去重、归类和标准化
→ 机器学习数据导出

1. **图表处理必须先于 DFT 数据处理。**
2. 图表解析支持两条路径：
   - 本地 AI 直接处理图表审核包；
   - 导出材料交给网页 GPT 解析，再校验并回传 JSON。
3. JSON 校验不得写库；只有用户明确应用结果后才能写库。应用图表结果不代表整篇论文已经完成。
4. 所有图表、DFT 记录和证据必须严格限定在当前 `paper_id` 以及该论文明确关联的 SI 内，不得跨文献混用证据。
5. 证据不足、来源不明或科研含义不能唯一确定时，必须标记 `blocked` 并说明缺失证据，禁止猜测、伪造或强制完成。
6. 审核页面只展示真实问题、证据、状态和进度，不自动创建 AI 任务、不自动轮询、不自动裁决、不自动标记完成。
7. 在单篇论文完整闭环真正跑通以前，禁止把批量队列、全库自动执行器和机理解析作为开发重点。
8. 任何代码修改前都必须重新阅读本规则，并逐项确认该修改是否直接服务于上述单篇流程；不服务于该流程的修改不得实施。
9. 每完成一个文件的修改，都必须再次对照本规则检查是否发生目标偏移，然后才能修改下一个文件。

### 单篇论文标准流程

1. 选择一篇论文，只处理当前 `paper_id`；主文和它明确关联的 SI 视为同一个证据包。
2. 确认主文和 SI 的 PDF 已完成解析，能够读取正文、页码、图片和表格；解析失败时停止后续流程。
3. 先解析并审核当前论文全部应审核图表，使用本地 AI 或“导出材料 → 网页 GPT → JSON 回传”路径。
4. 先校验图表结果，再由用户明确应用。每个图表必须保留来源论文、图号或表号、PDF 页码、对象、原文标题或说明、提取结论和证据定位状态。
5. 图表阶段必须覆盖全部应审核图表；证据不足的项目保持 `blocked`，不得为进入下一阶段而强制完成。
6. 图表完成后，AI 才能基于主文、SI 及已核验图表和表格提取 DFT 数据。
7. 每条 DFT 数据必须回到真实 PDF 核验数值、单位、催化剂或材料、双金属组成、活性位点、配位环境、吸附物或反应步骤、数据类型、来源论文、PDF 页码及对应图表或原文证据。
8. 能由证据唯一确定的问题由 AI 修正并记录依据；不能唯一确定的问题保持 `blocked`，页面不得提供自动科研裁决入口。
9. 仅在当前 `paper_id` 内进行去重、归类和标准化；保留原始值和原始单位，不得合并不同结构、位点、吸附物、反应步骤或计算条件的数据。
10. 写入后必须重新读取后端真实数据，确认图表状态、DFT 核验状态、`blocked` 项、证据定位、去重结果和标准化结果；页面不得自行宣告完成。
11. 只有证据完整、核验通过且字段满足要求的 DFT 数据才能进入机器学习数据出口；`blocked`、来源不明、单位不明、材料身份不明或不属于目标催化剂范围的数据不得进入训练集。

## 接手第一步（任何 AI 先做这个）

1. 通读本文件；**服务器接入与免密登录见 `SERVER_ACCESS.md`**（`ssh litai` 直接免密连服务器，真正工作区在服务器 `/opt/literature-ai`，不在本地仓库）；涉及凭据/备份细节再读 `local/srv_deploy/README.md`（gitignore，不外传）。
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

### 单个静态前端文件发布（强制最短路径）

- `literature-ai/frontend/` 已挂载到 backend 容器的 `/frontend`。如果本次只修改一个或少量 HTML/CSS/原生 JS 文件，且不涉及后端代码、依赖、构建产物、Compose、Nginx 或环境变量，必须使用 `local/srv_deploy/sshkit.py put` 将目标文件直接上传到 `/opt/literature-ai/frontend/` 的对应路径；文件上传后立即生效。
- 上述场景禁止默认执行 `update.sh`、`docker compose up/restart`、重建容器或准备离线 Git bundle。只有变更确实依赖这些步骤时才允许使用，并须先说明原因。
- 发布后至少校验本机与服务器目标文件的 SHA-256 一致，并检查 backend 健康状态；能进行浏览器验证时，再核对实际页面效果。
- Git commit/push 用于保存代码历史，但不得把服务器从 GitHub 拉取成功作为单个静态前端文件生效的前置条件。服务器暂时无法访问 GitHub时，先完成精确文件发布，再如实报告仓库同步状态。
- 精确上传失败时先报告具体错误，不得未经说明自动升级为全量部署或复杂离线发布方案。

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
## OAuth 部署状态与文件保护（2026-09-09 起，强制）

1. **服务器已上线 ChatGPT 连接器 OAuth 2.1 层**（2026-09-09，GitHub 部署分支 HEAD `0adaf4f`）。以下文件以服务器 `/opt/literature-ai` 与部署分支为权威，**禁止任何 AI 用本地旧版本覆盖服务器新版本**：
   - `backend/app/main.py`、`backend/app/config.py`、`backend/app/mcp/auth.py`、`backend/app/oauth.py`
   - `docker-compose.yml`（OAuth 环境变量在 `x-app-environment` 锚点，共 8 项 `LITAI_OAUTH_*`）
   - `deploy/nginx/owner.conf.template`（`/oauth`、`/.well-known` 两个 location 豁免 Basic 并转发）
   - `.env`（`LITAI_OAUTH_*` 8 项；update.sh 保护 .env 不覆盖，但改 .env 必须走备份 + force-recreate backend）
2. **本地仓库与服务器已分叉**：`AI-shujvku-slimming` 工作区的 `main.py` 仍引用已废弃的 `app.api.intake` 模块（服务器版没有），该工作区 `deploy/nginx/owner.conf.template` 是损坏 GBK 编码。**禁止把工作区版 main.py / config.py / nginx 模板上传服务器**。
3. **修改上述文件前的强制动作**：先 `scp litai:/opt/literature-ai/<同一路径>` 拉取服务器当前版并与本地 diff；本地较旧或结构不同时以服务器为准，在服务器版基础上改；上传后必须校验文件与服务器运行态（restart/force-recreate 容器、curl 公网入口）一致。
4. **部署验证一律以服务器运行态和用户实际看到的页面为准**（见顶部"顶级规则"），不以本地测试、Git 状态或文件哈希代替。
5. 服务器当前关键状态（2026-09-09）：OAuth 登录用户 `liyuhao`（密码在 `.env` 的 `LITAI_OAUTH_LOGIN_PASS`）；回调白名单 `https://chatgpt.com/connector/oauth/fj_Yn3qj7Yur`；静态 MCP key 已泄露待轮换（用户暂未批准）。

