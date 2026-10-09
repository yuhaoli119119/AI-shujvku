#!/bin/bash
# =====================================================================
# focused-suite —— 跑「定向回归」18 个 spec，产出可对比的 JSON
# ---------------------------------------------------------------------
# 用法：
#   tools/focused-suite.sh                          # 默认安全源 http://127.0.0.1:4173
#   tools/focused-suite.sh http://127.0.0.1:4173 /tmp/litai-uicheck/focused-current.json
#
# 然后与冻结基线逐条对比：
#   node tools/focused-diff.js tools/baselines/focused-2026-09-21.json <上面的 OUT>
#
# 必须用「安全源」127.0.0.1:4173（playwright.config.static.js 自带 npm run test:serve）：
# 用 http://172.18.0.6:8000 这类非安全源时 navigator.clipboard 不可用，
# 会让审核中心 4 个"复制命令"用例假失败。
#
# 环境变量：WORKERS（默认 6）、TEST_BASE_URL 由本脚本第一个参数设置。
# 基线里 29 个 unexpected 是既有环境性失败（改动前就有），关键看「0 新增失败」。
# =====================================================================
set -u
BASE_URL="${1:-http://127.0.0.1:4173}"
OUT="${2:-/tmp/litai-uicheck/focused-current.json}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" || exit 2

SPECS="tests/paper_detail_ui_static.spec.js tests/literature_library_optimization_static.spec.js tests/literature_library_tab_simplification_static.spec.js tests/literature_library_ui_simplification_static.spec.js tests/literature_library_recovery_static.spec.js tests/workspace_ui_static.spec.js tests/dft_database_profile_export.spec.js tests/dft_complete_loading.spec.js tests/review_center_title_metadata_static.spec.js tests/review_center_issues_ai.spec.js tests/review_center_conflict_modal.spec.js tests/chart_review_flow_static.spec.js tests/layout.spec.js tests/test_infrastructure_static.spec.js tests/dft_detail_deeplink.spec.js tests/pdf_availability_static.spec.js tests/review_discoverability_static.spec.js tests/optimization_p1_p2_fixes.spec.js"

echo "# focused 定向回归 | BASE=$BASE_URL | WORKERS=${WORKERS:-6} | OUT=$OUT"
start=$(date +%s)
TEST_BASE_URL="$BASE_URL" timeout 1800 node node_modules/@playwright/test/cli.js test $SPECS \
  --config=playwright.config.static.js --workers="${WORKERS:-6}" --reporter=json > "$OUT" 2>"$OUT.err"
code=$?
echo "# exit=$code 用时 $(( $(date +%s) - start ))s"
echo "# 结果：$OUT（stderr：$OUT.err）"
echo "# 下一步：node tools/focused-diff.js tools/baselines/focused-2026-09-21.json $OUT"
# 29 个既有失败会让 playwright 返回 1，这是预期的；真正的判据是 focused-diff 的退出码。
exit 0
