#!/usr/bin/env bash
# =====================================================================
# ui-check.sh —— LitAI 工作台 UI 快跑入口（响应式 / 链接 / 跨页跳转）
# ---------------------------------------------------------------------
# 用法：
#   frontend/tools/ui-check.sh verify         # ★日常开关：spot + links + jumps(QUICK)，实测约 6 分钟（37s+2m11s+3m27s）
#   frontend/tools/ui-check.sh full           # 17 页 × 6 档响应式扫描（并行，约 3-4 分钟）
#                                             #   不给参数时自动与 tools/baselines/ 里最新基线逐条对比
#   frontend/tools/ui-check.sh full B.json    # 同上，但指定基线文件
#   frontend/tools/ui-check.sh baseline [名]  # 把最近一次 full 结果冻结成新基线（含量法元数据）
#   frontend/tools/ui-check.sh spot [页,...]  # 快速抽查（默认 6 个高频页 × 390/1024，约 1 分钟）
#   frontend/tools/ui-check.sh links          # 全站内部链接体检（160 个唯一目标逐个请求，实测 2m11s）
#   frontend/tools/ui-check.sh jumps          # 8 个入口真实点击 → 核对返回是否回来源页（实测 3m27s；QUICK=1 约 2m）
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
BASELINE_DIR="$HERE/baselines"
mkdir -p "$TMPDIR_UI" "$BASELINE_DIR"
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
    ln -sfn "$OUT" "$TMPDIR_UI/full.latest"
    BASE_JSON="${1:-}"
    if [ -z "$BASE_JSON" ]; then
      BASE_JSON="$(ls -1t "$BASELINE_DIR"/*.json 2>/dev/null | grep -v manifest | head -1 || true)"
      [ -n "$BASE_JSON" ] && echo "# 未指定基线，使用最新冻结基线：$BASE_JSON"
    fi
    if [ -n "$BASE_JSON" ] && [ -f "$BASE_JSON" ]; then
      node "$HERE/audit-diff.js" "$BASE_JSON" "$OUT"
      drc=$?
      # 量法不一致会让对比失效，必须显式提醒
      node -e '
        const fs=require("fs");
        const g=p=>{try{return JSON.parse(fs.readFileSync(p.replace(/\.json$/,"")+".meta.json","utf8"))}catch(_){return null}};
        const a=g(process.argv[1]), b=g(process.argv[2]);
        const bad=[];
        if(a&&b){
          if(a.settle!==b.settle) bad.push("SETTLE "+a.settle+" vs "+b.settle);
          if(a.stable!==b.stable) bad.push("STABLE "+a.stable+" vs "+b.stable);
          if(String(a.sizes)!==String(b.sizes)) bad.push("SIZES 不同");
          if(String(a.pages)!==String(b.pages)) bad.push("PAGES 不同");
        }
        if(bad.length) console.log("# ⚠ 量法不一致，对比可能失真: " + bad.join(" / "));
      ' "$BASE_JSON" "$OUT"
      [ "$drc" -ne 0 ] && rc=1
    else
      echo "# 还没有冻结基线；确认这批结果没问题后跑： $0 baseline"
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
  verify)
    SECONDS=0
    echo "### 1/3 抽查响应式（spot）"
    OUT="$TMPDIR_UI/spot-$STAMP.json" PAGES="$SPOT_PAGES" SIZES="$SPOT_SIZES" node "$HERE/audit-responsive.js"; rc1=$?
    echo "### 2/3 链接体检（links）"
    OUT="$TMPDIR_UI/links-$STAMP.json" node "$HERE/link-audit.js"; rc2=$?
    echo "### 3/3 跨页返回核对（jumps, QUICK=1 跳过慢入口）"
    OUT="$TMPDIR_UI/jumps-$STAMP.json" QUICK=1 node "$HERE/return-nav-check.js"; rc3=$?
    echo "# verify 汇总：spot=$([ $rc1 -eq 0 ] && echo PASS || echo FAIL) links=$([ $rc2 -eq 0 ] && echo PASS || echo FAIL) jumps=$([ $rc3 -eq 0 ] && echo PASS || echo FAIL) 用时=$((SECONDS/60))m$((SECONDS%60))s"
    echo "# 结果：$TMPDIR_UI/{spot,links,jumps}-$STAMP.json"
    [ $rc1 -eq 0 ] && [ $rc2 -eq 0 ] && [ $rc3 -eq 0 ] || exit 1
    exit 0
    ;;
  baseline)
    NAME="${1:-ui-$STAMP}"
    SRC_JSON="${2:-}"
    if [ -z "$SRC_JSON" ]; then
      SRC_JSON="$(ls -1t "$TMPDIR_UI"/full-*.json 2>/dev/null | head -1 || true)"
      [ -z "$SRC_JSON" ] && SRC_JSON="$(ls -1t "$TMPDIR_UI"/spot-*.json 2>/dev/null | head -1 || true)"
    fi
    [ -n "$SRC_JSON" ] && [ -f "$SRC_JSON" ] || { echo "找不到可冻结的结果，请先跑 $0 full"; exit 2; }
    cp "$SRC_JSON" "$BASELINE_DIR/$NAME.json"
    [ -f "${SRC_JSON%.json}.meta.json" ] && cp "${SRC_JSON%.json}.meta.json" "$BASELINE_DIR/$NAME.meta.json"
    node -e '
      const fs=require("fs"), p=require("path");
      const dir=process.argv[1], name=process.argv[2];
      const f=p.join(dir,"manifest.json");
      let m={baselines:[]}; try{m=JSON.parse(fs.readFileSync(f,"utf8"))}catch(_){}
      m.baselines=(m.baselines||[]).filter(b=>b.name!==name);
      let meta=null; try{meta=JSON.parse(fs.readFileSync(p.join(dir,name+".meta.json"),"utf8"))}catch(_){}
      const rows=JSON.parse(fs.readFileSync(p.join(dir,name+".json"),"utf8"));
      m.baselines.push({name, frozenAt:new Date().toISOString(), records:rows.length, meta});
      fs.writeFileSync(f, JSON.stringify(m,null,1));
      console.log(`# 已冻结基线 ${name}: ${rows.length} 条${meta?`（SETTLE=${meta.settle} STABLE=${meta.stable} 并发=${meta.workers} BASE=${meta.base}）`:""}`);
    ' "$BASELINE_DIR" "$NAME"
    echo "# 之后 $0 full 不带参数就会自动与它对比"
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
    sed -n '2,24p' "${BASH_SOURCE[0]}"
    exit 2
    ;;
esac
