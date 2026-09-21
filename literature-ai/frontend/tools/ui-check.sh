#!/usr/bin/env bash
# =====================================================================
# ui-check.sh —— LitAI 工作台 UI 快跑入口（响应式 / 链接 / 跨页跳转）
# ---------------------------------------------------------------------
# 用法：
#   frontend/tools/ui-check.sh full           # 17 页 × 6 档响应式扫描（并行，约 4-6 分钟）
#   frontend/tools/ui-check.sh full BASE.json # 同上，并与基线逐条对比（有差异退出码 1）
#   frontend/tools/ui-check.sh spot [页,...]  # 快速抽查（默认 6 个高频页 × 390/1024，约 1 分钟）
#   frontend/tools/ui-check.sh links          # 全站内部链接体检（403 目标逐个请求，约 2 分钟）
#   frontend/tools/ui-check.sh jumps          # 8 个入口真实点击 → 核对返回是否回来源页（约 3 分钟）
#   frontend/tools/ui-check.sh shots /tmp/x   # 全页截图
#   frontend/tools/ui-check.sh diff A.json B.json
# 可覆盖的环境变量：BASE（默认 http://172.18.0.6:8000，backend 只读通道）、
#   WORKERS（默认 min(8,CPU)）、SETTLE（默认 6000）、PAGES、SIZES、OUT、QUICK=1（jumps 跳过慢入口）
# 提醒：SETTLE 会改变量到的高度；和历史基线对比时必须用基线当时的 SETTLE（FINAL14/15 为 6000）。
# =====================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${BASE:-http://172.18.0.6:8000}"
export BASE
TMPDIR_UI="${UI_CHECK_TMP:-/tmp/litai-uicheck}"
mkdir -p "$TMPDIR_UI"
STAMP="$(date +%Y%m%d-%H%M%S)"

# 抽查清单：改动影响面最大、最容易回归的页面（按需改这一行即可）
SPOT_PAGES="${SPOT_PAGES:-paper_detail,literature_library,review_center,dft_database,literature_screening,mechanism_knowledge}"
SPOT_SIZES="${SPOT_SIZES:-390x844,1024x768}"
FULL_PAGES="ai_writer,content_knowledge,dashboard,dft_audit_center,dft_database,external_analysis_workbench,extraction_workflow,ingestion,literature_library,literature_screening,mechanism_knowledge,paper_detail,review_center,settings,visuals,readonly,share"
FULL_SIZES="390x844,768x1024,820x1180,1024x768,1280x900,1440x900"

mode="${1:-full}"; shift || true

case "$mode" in
  full)
    OUT="${OUT:-$TMPDIR_UI/full-$STAMP.json}"
    PAGES="$FULL_PAGES" SIZES="$FULL_SIZES" OUT="$OUT" node "$HERE/audit-responsive.js"
    rc=$?
    echo "# 结果 JSON: $OUT（用时/问题数见上方汇总）"
    if [ "$rc" -eq 0 ]; then
      LL="$TMPDIR_UI/full-$STAMP.latest"; ln -sfn "$OUT" "$LL"
      echo "# 已更新软链 $LL -> $OUT"
    fi
    if [ -n "${1:-}" ] && [ -f "${1:-}" ]; then
      node "$HERE/audit-diff.js" "$1" "$OUT"
      rc=$?
    fi
    exit "$rc"
    ;;
  spot)
    PAGES="${1:-$SPOT_PAGES}"
    OUT="${OUT:-$TMPDIR_UI/spot-$STAMP.json}"
    echo "# 抽查页: $PAGES | 档位: $SPOT_SIZES"
    PAGES="$PAGES" SIZES="$SPOT_SIZES" OUT="$OUT" node "$HERE/audit-responsive.js"
    rc=$?
    echo "# 结果 JSON: $OUT"
    exit "$rc"
    ;;
  links)
    OUT="${OUT:-$TMPDIR_UI/links-$STAMP.json}"
    OUT="$OUT" node "$HERE/link-audit.js"
    rc=$?
    echo "# 结果 JSON: $OUT"
    exit "$rc"
    ;;
  jumps)
    OUT="${TMPDIR_UI}/jumps-$STAMP.json" node "$HERE/return-nav-check.js"
    rc=$?
    echo "# 结果 JSON: ${TMPDIR_UI}/jumps-$STAMP.json"
    exit "$rc"
    ;;
  shots)
    DIR="${1:-$TMPDIR_UI/shots-$STAMP}"
    echo "# 截图目录: $DIR"
    SHOTS="$DIR" node "$HERE/audit-responsive.js"
    exit $?
    ;;
  diff)
    [ -n "${1:-}" ] && [ -n "${2:-}" ] || { echo "用法: ui-check.sh diff A.json B.json"; exit 2; }
    node "$HERE/audit-diff.js" "$1" "$2"
    exit $?
    ;;
  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}"
    exit 2
    ;;
esac
