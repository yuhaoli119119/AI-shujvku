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

## 定向回归（focused）与功能专项验收

`ui-check.sh` 之外还有两个入口，覆盖"结构性回归"之外的断言：

| 命令 | 作用 | 实测耗时 |
|---|---|---|
| `tools/focused-suite.sh [源] [OUT]` | 跑 18 个 spec 文件共 71 条既有测试，输出 Playwright JSON | 约 4–5 分钟 |
| `node tools/focused-diff.js <基线> <当前>` | 逐条对比 spec 状态；两种输入都吃（原始报告或紧凑摘要） | 秒级 |
| `node tools/filter-return-check.js` | 筛选→进详情→返回的状态还原、冷启动带参 URL、`return_to` 开放重定向、screening 显式应用语义；`ONLY=<页名>` 可只跑一页 | 全量约 10 分钟 / 单页约 2 分钟 |

**跑 focused 必须用安全源 `127.0.0.1:4173`**（`playwright.config.static.js` 自带
`npm run test:serve`）；用 `172.18.0.6:8000` 会让审核中心 4 个"复制命令"用例假失败。
基线 `baselines/focused-2026-09-21.json` 里有 29 个 `unexpected` 是**既有环境性失败**，
判据是「0 新增失败」，不是"全绿"。

冻结基线里的紧凑摘要（`*.json` + `specs: [{file,title,status}]`）由原始 Playwright 报告压掉
stdout 得来（3.4M → 13K），`focused-diff.js` 两种格式都能读。
2026-09-21 那次的全部证据与逐条结论见 `baselines/evidence-2026-09-21/README.md`。

## 旋钮（环境变量）

`BASE`（被测源，默认 `http://172.18.0.6:8000`）、`WORKERS`（默认 min(8,CPU)）、
`SETTLE`（默认 6000ms，**与基线对比时必须一致**）、`STABLE`（默认 2500ms，"连续 2.5s 高度不变"
才算渲染完；设 0 = 关闭，等价旧脚本）、`QUICK=1`（jumps 跳过慢入口）、`SPOT_PAGES`、
`SPOT_SIZES`、`OUT`、`UI_CHECK_TMP`（结果目录，默认 `/tmp/litai-uicheck`）。
Playwright 的 `outputDir` 由 `PW_OUT_DIR` 控制（默认 `/tmp/litai-uicheck`）：临时目录写在
仓库外的 `/tmp`，不再往 `frontend/` 里堆 `pw-out-<pid>/` 污染 `git status`。

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
