# literature-ai 服务器接入与工作区说明（AI 必读）

> 本文件告诉接手的 AI：**真正的工作区在服务器上，不在本地**；以及如何免密登录、两条连接通道怎么用。
> 与 `AGENTS.md` 配套，先读 `AGENTS.md` 再读本文件。

## 1. 真正的工作区在服务器，不在本地

| | 服务器 | 本地仓库（本 slimming 仓库） |
|---|---|---|
| **角色** | 运行环境，实际跑的软件在 docker 容器里 | 源码副本（改代码的起点 + 读代码/搜索的快取） |
| **路径** | `/opt/literature-ai` → 软链真实路径 `/opt/AI-shujvku/literature-ai` | `D:\Desktop\03_代码与开发\AI-shujvku-slimming` |
| **能做** | 改配置 / 查数据 / 重启 / 看运行日志 / 改运行中的代码 | 改源码（→ push GitHub → 服务器 `update.sh` 拉）/ 读代码 / 本地 grep |
| **不能做** | — | 运行 / 测试（无 docker、无数据、无 PG） |

**铁律**：读 / 改 / 查**实际运行的软件**，一律 SSH 到服务器做，不要拿本地文件当真源。数据真源是服务器 PostgreSQL `literature_ai` 库。

两边源码版本一致（`e667b1b2 chore: finish repository slimming`），本地 working tree 干净；服务器唯一本地改动是 `update.sh` 被加了可执行位（权限差异，内容一致）。

## 2. 免密登录（已配好，所有 AI 会话直接用）

本机 `~/.ssh/litai_ed25519`（私钥）+ 服务器 `/root/.ssh/authorized_keys`（公钥）已配对。**任何本机 AI 会话**都能直接免密登录，不需要 `cred.env`、不需要 `sshkit.py`、不需要密码。

### 默认：走远程通道（公网，不依赖局域网）

```bash
ssh litai '<远程命令>'
```

- 等价于 `ssh -i ~/.ssh/litai_ed25519 root@100.70.12.57 -p 2223 '<命令>'`
- 经 Tailscale 公网 overlay，**不在内网也能连**
- 已配 `ControlMaster auto` + `ControlPersist 10m`：首次连后 10 分钟内复用 socket，后续命令**秒级响应**

### 备用：局域网通道（仅内网）

```bash
ssh litai-lan '<远程命令>'
```

- 等价于 `ssh -i ~/.ssh/litai_ed25519 root@192.168.110.229 '<命令>'`
- 仅在 192.168 内网可达时用

## 3. 两条通道对比

| 通道 | Host | 地址 | 用户 | 依赖 | 用途 |
|---|---|---|---|---|---|
| 远程 | `litai` | `100.70.12.57:2223` | root | Tailscale（公网） | **默认**，随时随地 |
| 局域网 | `litai-lan` | `192.168.110.229:22` | root | 内网 | 备用 / Tailscale 断时 |

两通道共享同一把 `litai_ed25519` 私钥、同一个 root `authorized_keys`（远程 2223 转发到服务器 22）。

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

- root 密码**只存在本机原仓库** `D:/Desktop/03_代码与开发/AI-shujvku/local/srv_deploy/cred.env`（gitignore，永不入库、永不贴进对话）。
- 免密用 SSH key（公钥在服务器 `authorized_keys`，私钥在 `~/.ssh/litai_ed25519`），**不涉及密码外传**。
- paramiko 备用通道：原仓库 `AI-shujvku/local/srv_deploy/sshkit.py`（系统 Python `D:/Python/python.exe` 自带 paramiko 4.0.0），用法 `cd 原仓库/local/srv_deploy && set -a && . ./cred.env && set +a && D:/Python/python.exe sshkit.py run '<cmd>' <timeout>`。仅在 `ssh litai` 不可用时降级使用。
- 吊销免密：删服务器 `/root/.ssh/authorized_keys` 里对应公钥行即可。

## 6. 给新 AI 的一句话

**"改运行态 `ssh litai`；改源码本地改完 push 让服务器 `update.sh` 拉；数据真源是服务器 PG `literature_ai` 库，动数据先备份。"**
