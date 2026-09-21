# 工作台 UI 快跑工具（`frontend/tools/`）

改完前端后用它证明"没把原来好的地方弄坏"，替代人工在手机/平板上逐页点。
所有脚本都在服务器上跑（被测站点用只读通道 `http://172.18.0.6:8000`），不需要外网。

## 最常用的一条

```bash
/opt/literature-ai/frontend/tools/ui-check.sh verify
```

`verify` = 抽查响应式（spot） + 全站链接体检（links） + 跨页返回核对（jumps，跳过慢入口），
三个都通过才返回 0。**实测总用时 6m15s**（spot 37s + links 2m11s + jumps 3m27s），
脚本结尾会打印真实用时。

## 全部模式

| 命令 | 作用 | 实测耗时 |
|---|---|---|
| `ui-check.sh verify` | 日常开关：spot + links + jumps(QUICK) | 6m15s（实测） |
| `ui-check.sh spot [页,...]` | 快速抽查（默认 `SPOT_PAGES` 6 页 × 390/1024） | 约 40 秒 |
| `ui-check.sh full [基线.json]` | 17 页 × 6 档全量扫描；不给基线时自动用 `tools/baselines/` 最新基线对比 | 约 3–4 分钟（并行 8 路；串行约 25 分钟） |
| `ui-check.sh links` | 全站内部链接体检：唯一目标逐个请求、`paper_detail` 必须带 `paper_id`、统计每页"进详情"入口（160 个目标） | 2m11s（实测） |
| `ui-check.sh jumps` | 8 个入口真实点击进详情，断言面包屑/返回按钮/定位条/底部动作条都回来源页 | 3m27s（实测）；`QUICK=1` 约 2 分钟 |
| `ui-check.sh baseline [名] [结果.json]` | 把最近一次 full/spot 结果冻结成新基线 | 秒级 |
| `ui-check.sh shots <目录>` | 全页截图（全站 × 默认档位） | 约 3–4 分钟 |
| `ui-check.sh diff A.json B.json` | 任意两份结果逐条对比 | 秒级 |

## 基线机制

- 基线放在 `tools/baselines/*.json`，每条基线配一份 `*.meta.json`（记录当时的
  `SETTLE`/`STABLE`/并发/`BASE`/页面与档位清单）与 `manifest.json` 索引。
- `full` 不带参数 → 自动挑**最新**基线对比；有差异退出码 1，并逐条列出
  `sh/ovf/errors/bad` 的变化与新增/缺失记录。
- 若基线与本次的量法不同（`SETTLE`/`STABLE`/`PAGES`/`SIZES`），脚本会显式打印
  `⚠ 量法不一致，对比可能失真` —— 这时不要相信"0 差异"。
- 当前冻结基线两条：`frozen-2026-09-21a`（= 历史 `FINAL15.json`，无 meta）与
  `ui-2026-09-21`（同日用新脚本重跑、带 meta）。`full` 不传参时用**最新**的那条，
  两者内容逐条一致。

## 旋钮（环境变量）

`BASE`（被测源，默认 `http://172.18.0.6:8000`）、`WORKERS`（默认 min(8,CPU)）、
`SETTLE`（默认 6000ms，**与基线对比时必须一致**）、`STABLE`（默认 2500ms，"连续 2.5s 高度不变"
才算渲染完；设 0 = 关闭，等价旧脚本）、`QUICK=1`（jumps 跳过慢入口）、`SPOT_PAGES`、
`SPOT_SIZES`、`OUT`、`UI_CHECK_TMP`（结果目录，默认 `/tmp/litai-uicheck`）。

## 已知边界

- `dft_database`（约 24s 出表格）、`visuals`（等热力图自动选中）、`review_center` 是慢页面，
  `link-audit.js` 用 `PAGE_SETTLE` 表覆盖，漏了这张表会漏检它们的链接。
- `link-audit.js` 覆盖 `<a href>` 与 `tr[data-paper-id]` 两类入口；其他形态（按钮 onclick）不覆盖。
- 审计只能证明"结构没坏"（不溢出、不报错、跳转对、高度没突变），
  **不替代真机/浏览器里的人工验收**；字体大小、留白是否合适仍以人看为准。
- 每个"页面 × 档位"都用全新 browser context（避免 localStorage 串页影响高度），
  所以并行只是调度并行，不改变量法。

## 与历史脚本的关系

2026-09-21 之前的审计脚本散在 `/tmp/litai-uicheck/`（`audit.js` 等）。功能已全部并入这里：
`audit.js` → `audit-responsive.js`（`STABLE=0` 时行为等价），链接核对 → `link-audit.js`，
跨页返回 → `return-nav-check.js`。历史结果 `FINAL14.json`/`FINAL15.json` 仍可用于对比
（`audit-diff.js` 只按 `size|page` 逐条比字段，不关心来源脚本）。
