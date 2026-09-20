# literature-ai 服务器接入与工作区说明（AI 必读）

> 本文件告诉接手的 AI：**真正的工作区在服务器上，不在本地**；以及如何免密登录、两条连接通道怎么用。
> 与 `AGENTS.md` 配套，先读 `AGENTS.md` 再读本文件。

## 1. 真正的工作区在服务器，不在本地

下表描述的是**用户 Windows 本机**与服务器的关系；若 AI 直接运行在服务器宿主机（或同机 Agent Canvas 容器）中，请见 `AGENTS.md` 的"Agent Canvas / 服务器内 AI 的真实拓扑"一节——那种情况下源码目录与生产同机，不需要跨机同步。

| | 服务器 | Windows 本机仓库 |
|---|---|---|
| **角色** | 运行环境，实际跑的软件在 docker 容器里 | 源码副本（改代码的起点 + 读代码/搜索的快取） |
| **路径** | `/opt/literature-ai` → 软链真实路径 `/opt/AI-shujvku/literature-ai`；源码在工作树 `/opt/ai-shujvku-src` | `D:\Desktop\03_代码与开发\AI-shujvku-slimming` |
| **能做** | 改配置 / 查数据 / 重启 / 看运行日志 / 改运行中的代码 | 改源码（→ push GitHub → 服务器 `update.sh` 拉）/ 读代码 / 本地 grep |
| **不能做** | — | 运行 / 测试（无 docker、无数据、无 PG） |

**铁律**：读 / 改 / 查**实际运行的软件**，一律 SSH 到服务器做，不要拿本地文件当真源。数据真源是服务器 PostgreSQL `literature_ai` 库。

⚠️ 两边源码**未必一致**，不要假设版本相同：2026-09-17 实测服务器 `/opt/ai-shujvku-src` 处于 `codex/figure-library-recovery-20260915-061500`（HEAD `484a5358`）且工作树有 83 个未提交改动，与部署分支 `codex/content-knowledge-workbench-20260716` 已分叉。**以服务器实际 `git rev-parse HEAD` 与 `git status` 为准。**

## 2. 免密登录

私钥 `~/.ssh/litai_ed25519` + 服务器 `2401liyuhao` 账号的 `authorized_keys` 已配对。**已授权的会话**可直接免密登录，不需要 `cred.env`、不需要 `sshkit.py`、不需要密码。

> **2026-09-17 实测更正**：可用通道是**局域网 `/opt` 直连**，实测成功的账号是 **`2401liyuhao`**（不是 root）。
> Tailscale 通道 `100.70.12.57:2223` 在服务器上的 Agent Canvas 容器内**不可达**；`root` 账号用同一把密钥**认证被拒**。
> 请以实际测得的通道为准，不要照抄旧文档的 root/Tailscale 组合。

```bash
ssh litai '<远程命令>'        # 当前配置：2401liyuhao@192.168.110.229:22（局域网）
```

该账号 `sudo` 免密，运维权限实际等同 root（`sudo docker compose ...`、`sudo -i`）。

### 备用：Tailscale 公网通道（视网络环境而定）

```bash
ssh litai-ts '<远程命令>'
```

- 目标 `100.70.12.57:2223`
- 仅在能路由到 Tailscale overlay 的会话可用；**服务器本机容器内实测不通**。

## 3. 两条通道对比

| 通道 | Host | 地址 | 用户 | 依赖 | 用途 |
|---|---|---|---|---|---|
| 局域网 | `litai` | `192.168.110.229:22` | `2401liyuhao` | 内网 | **实测可用，默认** |
| Tailscale | `litai-ts` | `100.70.12.57:2223` | 视配置 | Tailscale（公网） | 仅特定会话可用 |

## 4. AI 常用操作（直接 ssh，无需中间工具）

```bash
# 看容器状态
ssh litai 'cd /opt/literature-ai && docker compose ps'

# 看后端日志
ssh litai 'cd /opt/literature-ai && docker compose logs --tail=100 backend'

# 进数据库
ssh litai 'docker exec -it literature-ai-postgres-1 psql -U literature_ai -d literature_ai'

# 改单个前端文件并即时生效（最短路径，不走 update.sh）
#   先本地改好，scp 上传：
scp 文件 litai:/opt/literature-ai/frontend/对应路径/
#   或用原仓库 sshkit.py put（见下）

# 改代码全量发布：本地改 → push GitHub → 服务器
ssh litai 'cd /opt/ai-shujvku-src && ./update.sh'

# 备份数据库（改数据前必做）
ssh litai 'cd /opt/literature-ai && docker exec literature-ai-postgres-1 pg_dump -Fc -U literature_ai literature_ai > /home/2401liyuhao/literature_ai_latest.dump'
```

## 5. 凭据与安全

- 免密用 SSH key（公钥在服务器 `2401liyuhao` 的 `~/.ssh/authorized_keys`，私钥在会话 `~/.ssh/litai_ed25519`），**不涉及密码外传**；该账号 `sudo` 免密，实际权限等同 root。
- 私钥**只落在已授权的会话/本机**：`local/srv_deploy/cred.env`（gitignore，永不入库、永不贴进对话）。**任何 AI 的私钥副本不得提交、不得外传、不得写进会 push 的文档。**
- paramiko 备用通道（仅 Windows 本机）：原仓库 `local/srv_deploy/sshkit.py`。仅在 `ssh litai` 不可用时降级使用。
- 吊销免密：删服务器 `~/.ssh/authorized_keys` 里对应公钥行即可（2026-09-17 该文件中含 `ai-shujvku@MUYV` 一行，即当前使用的密钥）。

## 6. 给新 AI 的一句话

**"改运行态 `ssh litai`（`2401liyuhao@192.168.110.229`）；若自己就跑在服务器宿主机/Agent Canvas 容器里，源码目录与生产同机、改完仍需 `update.sh` 才生效；数据真源是服务器 PG `literature_ai` 库，动数据先备份；任何敏感删除先经用户确认。"**
