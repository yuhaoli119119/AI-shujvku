# AI-shujvku — 项目与 Agent 协作指南

本仓库为**本地文献 AI 系统**（literature-ai，PostgreSQL+pgvector 为数据唯一真源），生产环境已部署到本地服务器。任何 AI（Codex / 豆包 / 其他 agent）在此仓库执行任务前，请先读本节。

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

## 工程注意

- Windows 本地执行远程命令时，PowerShell 会破坏 `/dev/null`、`$()`、`*` 等 → 远程 bash 逻辑写成 `.sh` 上传执行。
- 服务器不可达 HuggingFace / Docker Hub；docling 模型已本地缓存，解析新 PDF 无需外网。
- 本机 agent 会话 git 写对象被沙箱拦截，勿在 agent 会话内尝试 `git add`/`git commit`/`git push`（会报 Permission denied），统一走用户普通终端。
