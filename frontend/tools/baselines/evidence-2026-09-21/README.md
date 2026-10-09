# 验收证据 2026-09-21（URL 状态同步 + 按来源返回）

这批文件是「列表页筛选/分页/页签写进 URL，详情页按来源精确返回原筛选视图」这次改动的
验收证据（改动落在提交 `c13d0309` / `9812e33d`，工具目录 `527add64`）。
放在仓库里是因为原始证据原先只存在 `/tmp`，会被 `systemd-tmpfiles` 按 10 天规则清掉。

## 每个文件证明什么

| 文件 | 由什么命令产生 | 证明了什么 |
|---|---|---|
| `filter-return-2026-09-21.json` | `node tools/filter-return-check.js` | 5 个列表页往返/冷启动 + 11 条开放重定向 + 显式应用语义，`PASS=120 FAIL=0` |
| `acceptance-full-2026-09-21.log` | 同上（stdout 全文） | 逐条 PASS 原文与每页真实 URL/条数 |
| `spot-20260921-152628.json` | `tools/ui-check.sh verify` | 响应式抽查 12 条 `ovf=0 errs=0 bad=0` |
| `links-20260921-152628.json` | `tools/ui-check.sh verify` | 全站 160 个唯一链接目标，异常 0 |
| `jumps-20260921-152628.json` | `tools/ui-check.sh verify` | 8 个入口进详情后的返回目标核对，`PASS=7 FAIL=0 SKIP=1` |
| `verify-2026-09-21.log` | 同上（stdout 全文） | `spot=PASS links=PASS jumps=PASS 用时=6m15s` |
| `full-20260921-153650.json` | `tools/ui-check.sh full` | 17 页 × 6 档共 102 条：`ovf=0 errs=0 bad=0 fail=0` |
| `full-vs-baseline-2026-09-21.log` | 同上（stdout 全文） | 与冻结基线 `tools/baselines/ui-2026-09-21.json` **逐条 0 差异** |
| `focused-2026-09-21.log` | `tools/focused-suite.sh` + `tools/focused-diff.js` | 71 条 spec 与基线 `tools/baselines/focused-2026-09-21.json` **逐条一致，0 新增失败** |

## 复跑方式

```bash
cd /opt/literature-ai/frontend

# 1) 日常开关（6m15s）
tools/ui-check.sh verify

# 2) 全量响应式 + 与冻结基线比对（约 4 分钟）
tools/ui-check.sh full

# 3) 定向回归：必须用安全源 127.0.0.1:4173，否则审核中心 4 个"复制命令"用例假失败
tools/focused-suite.sh http://127.0.0.1:4173 /tmp/litai-uicheck/focused-current.json
node tools/focused-diff.js tools/baselines/focused-2026-09-21.json /tmp/litai-uicheck/focused-current.json

# 4) 本轮功能的专项验收（约 10 分钟；单页用 ONLY=<页名> 约 2 分钟）
node tools/filter-return-check.js
ONLY=literature_screening node tools/filter-return-check.js
```

## 注意

- 基线里 29 个 `unexpected` 是**既有环境性失败**（改动前就失败），判据是「0 新增失败」，不是"全绿"。
- `filter-return-check.js` 里有几个与库内容绑定的期望值（screening 99/79 条、DFT 33 条等），
  库内容变化后会失败，文件头注释写了用 `curl` 重新取值的办法。
- 这些 JSON 都是当时的"结构快照"，用于逐条比对；不能代替真机人工看字体和留白。
