#!/bin/bash
# =====================================================================
# literature-ai 服务器一键更新脚本
# 权威位置：/opt/ai-shujvku-src/update.sh（本仓库 deploy/scripts/update.sh 为同内容副本）
# 用法：cd /opt/ai-shujvku-src && ./update.sh
# 链路：git pull 最新代码 → 同步到 /opt/literature-ai（保护 .env / data / docling_cache）→ 重建应用容器
# 前置：本机已把代码 push 到 GitHub（分支 codex/content-knowledge-workbench-20260716）
# =====================================================================
set -euo pipefail

SRC=/opt/ai-shujvku-src
RUN=/opt/literature-ai
BRANCH=codex/content-knowledge-workbench-20260716

echo "==> [1/4] 拉取最新代码 (branch: $BRANCH)"
cd "$SRC"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> [2/4] 发布前检查生产目录的源码外差异"
RSYNC_EXCLUDES=(
  --exclude '.env'
  --exclude 'data/'
  --exclude 'node_modules/'
  --exclude '**/__pycache__/'
  --exclude '*.htpasswd'
  --exclude 'AGENTS.md'
  --exclude 'GEMINI.md'
)
preflight_output=$(mktemp /tmp/literature-ai-rsync-preflight.XXXXXX)
trap 'rm -f "$preflight_output"' EXIT
rsync -ain --delete "${RSYNC_EXCLUDES[@]}" "$SRC/literature-ai/" "$RUN/" > "$preflight_output"
if grep -Fq '*deleting' "$preflight_output"; then
  echo "ERROR: 生产目录存在源码仓库没有的文件；为防止恢复代码再次丢失，发布已停止。" >&2
  grep -F '*deleting' "$preflight_output" >&2
  echo "请先逐项审核以上冲突文件，并使用精确文件同步清单发布。" >&2
  exit 42
fi

echo "==> 已通过生产专有文件检查；开始同步（保护运行数据与服务器规则文件）"
rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$SRC/literature-ai/" "$RUN/"

echo "==> [3/4] 强制重建应用容器（backend / worker / worker-pdf / owner-gateway / share-gateway / public-gateway），确保加载新代码与 .env"
cd "$RUN"
docker compose up -d --no-deps --force-recreate backend worker worker-pdf owner-gateway share-gateway public-gateway

echo "==> [4/4] 等待健康检查"
sleep 5
docker compose ps

echo "==> 更新完成。验证：curl -s http://127.0.0.1:8000/api/health"
