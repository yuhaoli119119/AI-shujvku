#!/bin/bash
# =====================================================================
# literature-ai 服务器一键更新脚本
# 权威位置：/opt/ai-shujvku-src/update.sh（本仓库 literature-ai/deploy/scripts/update.sh
# 为同内容副本，两份必须逐字节一致且 Git mode 均为 100755，禁止只改一份）
# 用法：cd /opt/ai-shujvku-src && ./update.sh
#       默认部署分支 codex/figure-library-recovery-20260915-061500；
#       需要部署其他分支时必须显式指定，例如：
#       LITAI_DEPLOY_BRANCH=codex/content-knowledge-workbench-20260716 ./update.sh
# 执行顺序与安全边界：
#   1) 静态安全检查（只读，且在任何 Git 工作树切换之前完成）：SRC/RUN 存在、tracked 工作区干净、
#      运行目录保护项存在、exclude 集合完整、ls-remote 取得目标远端提交 sha；
#   2) 精确抓取目标远端 ref：git fetch --no-tags origin refs/heads/$BRANCH，读取 FETCH_HEAD，
#      必须与先前 ls-remote 的结果完全一致，否则退出并要求重新运行；
#   3) 切到目标本地分支，只允许 fast-forward 到该提交（不使用 git pull、不 reset --hard、
#      不覆盖本地分叉历史）；切换后 HEAD 必须精确等于目标远端 sha，否则退出；
#   4) 对最终工作树执行权威 rsync dry-run——发生在任何运行目录写入与容器操作之前；
#   5) 上面全部通过后，才执行真实 rsync 与容器重建。
# 前置：目标代码已 push 到 GitHub。
# =====================================================================
set -euo pipefail

SRC=/opt/ai-shujvku-src
RUN=/opt/literature-ai
DEFAULT_BRANCH=codex/figure-library-recovery-20260915-061500
BRANCH="${LITAI_DEPLOY_BRANCH:-$DEFAULT_BRANCH}"

# 运行数据与服务器规则文件的保护排除集合：dry-run 与真实同步必须使用同一个数组
RSYNC_EXCLUDES=(
  --exclude '.env'
  --exclude 'data/'
  --exclude 'node_modules/'
  --exclude '**/__pycache__/'
  --exclude '*.htpasswd'
  --exclude 'AGENTS.md'
  --exclude 'GEMINI.md'
  --exclude 'SERVER_ACCESS.md'
)

# 上表是必须生效的保护项，缺失任意一项即拒绝运行
REQUIRED_EXCLUDES=(
  '.env'
  'data/'
  'node_modules/'
  '**/__pycache__/'
  '*.htpasswd'
  'AGENTS.md'
  'GEMINI.md'
  'SERVER_ACCESS.md'
)

# 运行目录中允许被 rsync --delete 删除的路径（相对 $RUN 的 glob）。默认为空 =
# 任何删除都视为异常并阻断。确需放行时必须显式修改本数组并走代码评审，
# 不要为了让 preflight 通过而在此处或 exclude 里偷偷加放行项。
ALLOW_DELETE_PATTERNS=()

# 运行目录中必须存在的保护项（相对 $RUN）
REQUIRED_PROTECTED_PATHS=(
  '.env'
  'AGENTS.md'
  'GEMINI.md'
  'SERVER_ACCESS.md'
  'data'
)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# dry-run 输出文件：必须是全局变量，否则 EXIT trap 在函数返回后展开会触发 unbound variable
PREFLIGHT_OUTPUT=""
cleanup_preflight() {
  if [ -n "$PREFLIGHT_OUTPUT" ]; then
    rm -f "$PREFLIGHT_OUTPUT"
  fi
  return 0
}
trap 'cleanup_preflight || true' EXIT

is_allowed_delete() {
  local rel="$1"
  local pat
  if [ "${#ALLOW_DELETE_PATTERNS[@]}" -eq 0 ]; then
    return 1
  fi
  for pat in "${ALLOW_DELETE_PATTERNS[@]}"; do
    # ALLOW_DELETE_PATTERNS 刻意使用 glob 语法：此处未加引号的展开是有意为之
    # shellcheck disable=SC2254
    case "$rel" in
      $pat) return 0 ;;
    esac
  done
  return 1
}

# 唯一的 dry-run 删除检查实现：避免检查逻辑被复制后与真实同步发生漂移。
# dry-run 与实际 rsync 必须使用完全相同的源目录、目标目录、--delete 与 RSYNC_EXCLUDES。
run_dry_run_delete_check() {
  local phase="$1"
  local line rel
  local blocked_lines=()
  echo "    - [$phase] rsync dry-run（源 $SRC/literature-ai/ → 目标 $RUN/，--delete，排除集合与真实同步一致）"
  PREFLIGHT_OUTPUT=$(mktemp /tmp/literature-ai-rsync-preflight.XXXXXX)
  rsync -ain --delete "${RSYNC_EXCLUDES[@]}" "$SRC/literature-ai/" "$RUN/" > "$PREFLIGHT_OUTPUT"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    rel=${line#\*deleting}
    rel=${rel#"${rel%%[![:space:]]*}"}
    if ! is_allowed_delete "$rel"; then
      blocked_lines+=("$rel")
    fi
  done < <(grep -F '*deleting' "$PREFLIGHT_OUTPUT" || true)
  if [ "${#blocked_lines[@]}" -gt 0 ]; then
    echo "ERROR: [$phase] rsync --delete 将删除以下运行目录文件，且不在允许清单内；发布已停止（未写运行目录、未执行真实 rsync、未触碰容器）。" >&2
    printf '  - %s\n' "${blocked_lines[@]}" >&2
    echo "请逐项决定：纳入 git 源、显式列入保护排除项，或经评审后写入 ALLOW_DELETE_PATTERNS。" >&2
    exit 42
  fi
  echo "    - [$phase] dry-run 未发现未放行的删除"
}

echo "==> [0/5] 部署目标"
[ -d "$SRC/.git" ] || fail "找不到 git 源仓库：$SRC"
[ -d "$RUN" ] || fail "找不到运行目录：$RUN"
echo "    部署分支      : $BRANCH"
echo "    当前分支      : $(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
echo "    当前 HEAD     : $(git -C "$SRC" rev-parse HEAD)"

echo "==> [1/5] 静态安全检查（只读；全部发生在任何 Git 工作树切换之前）"

echo "    - 检查 git tracked 工作区是否干净"
dirty=$(git -C "$SRC" status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then
  printf '%s\n' "$dirty" >&2
  fail "git tracked 工作区不干净；已停止（未切换分支、未同步、未触碰容器）"
fi

echo "    - 检查运行目录必需保护项是否存在"
for path in "${REQUIRED_PROTECTED_PATHS[@]}"; do
  [ -e "$RUN/$path" ] || fail "运行目录缺少必需的保护项：$RUN/$path"
done

echo "    - 检查 rsync 排除集合是否完整"
for pat in "${REQUIRED_EXCLUDES[@]}"; do
  found=no
  for ((i = 0; i < ${#RSYNC_EXCLUDES[@]}; i++)); do
    if [ "${RSYNC_EXCLUDES[$i]}" = "--exclude" ] && [ "${RSYNC_EXCLUDES[$((i + 1))]:-}" = "$pat" ]; then
      found=yes
      break
    fi
  done
  [ "$found" = yes ] || fail "rsync 排除集合缺少保护项：$pat"
done

echo "    - 检查目标远端分支并取得目标提交"
set +e
remote_refs=$(git -C "$SRC" ls-remote --exit-code --heads origin "refs/heads/$BRANCH" 2>&1)
remote_rc=$?
set -e
case "$remote_rc" in
  0) ;;
  2) fail "远端 origin 不存在分支 $BRANCH；已停止（未切换分支、未同步、未触碰容器）" ;;
  *) fail "查询远端 origin 失败（rc=$remote_rc）：$remote_refs" ;;
esac
REMOTE_SHA=$(printf '%s\n' "$remote_refs" | awk 'NR==1{print $1}')
[ -n "$REMOTE_SHA" ] || fail "未能解析目标远端提交 sha：$remote_refs"
echo "    目标远端 ref  : refs/heads/$BRANCH -> $REMOTE_SHA"

echo "==> [2/5] 精确抓取目标远端 ref 并校验"
git -C "$SRC" fetch --no-tags origin "refs/heads/$BRANCH"
FETCHED_SHA=$(git -C "$SRC" rev-parse FETCH_HEAD)
echo "    FETCH_HEAD    : $FETCHED_SHA"
if [ "$FETCHED_SHA" != "$REMOTE_SHA" ]; then
  fail "远端分支在检查期间发生变化（ls-remote=$REMOTE_SHA，FETCH_HEAD=$FETCHED_SHA）；已停止，请重新运行"
fi
echo "    校验结果      : FETCH_HEAD 与目标远端 ref 完全一致"

echo "==> [3/5] 切换到目标提交（只允许 fast-forward；不使用 git pull、不 reset --hard）"
if git -C "$SRC" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "    本地分支已存在，执行 checkout + merge --ff-only"
  git -C "$SRC" checkout "$BRANCH" || fail "切换到本地分支 $BRANCH 失败；已停止"
  git -C "$SRC" merge --ff-only "$FETCHED_SHA" || fail "本地分支 $BRANCH 无法 fast-forward 到 $FETCHED_SHA（存在本地分叉）；已停止（未同步、未触碰容器）"
else
  echo "    本地分支不存在，基于 $FETCHED_SHA 创建同名分支"
  git -C "$SRC" checkout -b "$BRANCH" "$FETCHED_SHA" || fail "基于 $FETCHED_SHA 创建本地分支 $BRANCH 失败；已停止"
fi
echo "    切换后分支    : $(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
echo "    切换后 HEAD   : $(git -C "$SRC" rev-parse HEAD)"
if [ "$(git -C "$SRC" rev-parse HEAD)" != "$REMOTE_SHA" ]; then
  fail "切换后 HEAD 与目标远端提交不一致（期望 $REMOTE_SHA）；已停止（未同步、未触碰容器）"
fi
echo "    校验结果      : HEAD 与目标远端 ref 完全一致"

echo "==> [4/5] 对最终工作树执行权威 rsync dry-run（发生在任何运行目录写入与容器操作之前）"
run_dry_run_delete_check "最终工作树"

echo "==> [5/5] 同步到运行目录（与权威 dry-run 使用同一 exclude 集合，保护运行数据与服务器规则文件）"
rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$SRC/literature-ai/" "$RUN/"

echo "==> [5/5] 强制重建应用容器（backend / worker / worker-pdf / owner-gateway / share-gateway / public-gateway），确保加载新代码与 .env"
cd "$RUN"
docker compose up -d --no-deps --force-recreate backend worker worker-pdf owner-gateway share-gateway public-gateway

echo "==> 等待健康检查"
sleep 5
docker compose ps

echo "==> 更新完成。验证：curl -s http://127.0.0.1:8000/api/health"
