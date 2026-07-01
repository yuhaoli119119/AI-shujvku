# 服务器优先 GitHub 工作流

本说明用于后续 AI / 协作者在服务器上继续维护 `literature-ai`，避免把“服务器代码”“GitHub 代码”和“运行期数据”混为一谈。

## 1. 当前约定

- 以后默认在**服务器**上改代码。
- 代码通过 GitHub 同步回本地 Windows。
- PostgreSQL、`data/`、`outputs/`、`.env` 不通过 GitHub 同步。
- 未经用户明确要求，不要再用本地副本反向覆盖服务器数据库或运行数据。

## 2. 关键路径

- Git 仓库根目录：`/opt/AI-shujvku`
- 应用稳定入口：`/opt/literature-ai`
- 实际目录：`/opt/literature-ai -> /opt/AI-shujvku/literature-ai`
- 旧部署快照：`/opt/literature-ai_legacy_20260702`

说明：

- 改代码、`git status`、`git commit`、`git push` 建议在 `/opt/AI-shujvku` 执行。
- `docker compose`、`.env`、`data/`、`outputs/`、服务重启与健康检查建议在 `/opt/literature-ai` 执行。

## 3. GitHub 连接方式

当前服务器已经配置好以下内容：

- `origin = git@github.com:yuhaoli119119/AI-shujvku.git`
- `~/.ssh/id_ed25519_github`
- `~/.ssh/config` 中 `Host github.com` 走 `ssh.github.com:443`

不要默认改回 HTTPS。该服务器上 GitHub HTTPS 曾出现超时、空回复、HTTP/2 异常；SSH over 443 更稳定。

可用自检命令：

```bash
ssh -T git@github.com
cd /opt/AI-shujvku
git ls-remote origin HEAD
```

正常时应看到：

- `You've successfully authenticated`
- `git ls-remote` 返回远端 commit

## 4. 每次开始前

先在仓库根目录执行：

```bash
cd /opt/AI-shujvku
git status --short
git log -1 --oneline
git branch -vv
```

如果工作区不干净、分支不对、或 HEAD 异常，先说明再继续。

## 5. 标准改动流程

### 5.1 改代码

```bash
cd /opt/literature-ai
# 在这里修改 backend / frontend / docs / prompts 等代码
```

### 5.2 提交并推送

```bash
cd /opt/AI-shujvku
git add -A
git commit -m "your message"
git push
```

### 5.3 重启服务

```bash
cd /opt/literature-ai
docker compose restart backend worker worker-pdf share-gateway owner-gateway
```

如果改动影响镜像构建、依赖或 compose 结构，再改用：

```bash
cd /opt/literature-ai
docker compose up -d --build
```

## 6. 推荐验收

```bash
cd /opt/literature-ai
docker compose ps
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/system/agent-guide
```

需要检查分享网关时再补：

```bash
curl http://127.0.0.1:8080/api/health
```

## 7. 数据边界

以下内容不是 GitHub 同步对象：

- PostgreSQL 数据库
- `literature-ai/data/`
- `literature-ai/outputs/`
- `literature-ai/.env`
- Docker volume

因此：

- 不要因为 `git pull` 成功，就声称数据库和运行数据也已同步。
- 不要把本地 Windows 的 `data/`、数据库 dump、`.env` 自动覆盖服务器。
- 如果用户明确要求做数据库或运行数据同步，必须单独说明影响范围并单独执行。

## 8. 服务器本机特例

- `literature-ai/docker-compose.override.yml` 是服务器保留的本机运行配置。
- 它当前未纳入 Git 跟踪，并通过 `.git/info/exclude` 屏蔽 `git status` 噪音。
- 未经明确要求，不要删除、重命名或提交它。

## 9. 本地副本的定位

本地 Windows 仓库现在主要用于：

- 拉取服务器已经 push 到 GitHub 的代码
- 本地阅读、测试、备份或辅助分析

默认不再把“本地代码改完再手动同步到服务器”当作主流程。
