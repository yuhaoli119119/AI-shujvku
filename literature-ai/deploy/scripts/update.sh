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

echo "==> [2/4] 同步代码到运行目录（保护 .env / data / docling_cache）"
rsync -av --delete \
  --exclude '.env' \
  --exclude 'data/' \
  --exclude 'node_modules/' \
  --exclude '**/__pycache__/' \
  "$SRC/literature-ai/" "$RUN/"

echo "==> [3/4] 强制重建应用容器（backend / worker / worker-pdf / owner-gateway / share-gateway / public-gateway），确保加载新代码与 .env"
cd "$RUN"
docker compose up -d --no-deps --force-recreate backend worker worker-pdf owner-gateway share-gateway public-gateway

echo "==> [4/4] 等待健康检查"
sleep 5
docker compose ps

echo "==> 更新完成。验证：curl -s http://127.0.0.1:8000/api/health"
