# AGENTS.md

本文件定义 `literature-ai` 的 AI 协作者最低协作规则。目标是减少误操作、减少误报、减少对当前 D2 数据底座状态的误导。

## 0. 当前基线

- **PostgreSQL + pgvector** 是当前唯一的 source of truth 和活跃业务库。
- SQLite 已经被全面弃用，不再是活跃数据库。
- 默认不改 canonical registry。
- 默认不删除真实 `data/`、`artifacts/`、shadow report。
- **27-Tool MCP 系统** 已全面激活，涵盖提取、裁切、审核流程。

## 1. 每轮开始前必须执行

每次进入任务前，先在仓库根目录执行并回报结果：

```bash
git status --short
git log -1 --oneline
git branch -vv
```

如发现工作区非空、HEAD 不符合预期、或分支异常，先说明，再继续。

## 2. 每轮结束时必须回报

无论是否改了代码，都要明确回报：

- 跑了哪些测试，结果是什么
- 是否有 commit，commit hash 是什么
- 是否已经 push
- 剩余风险、未验证项、假设项是什么

不要把“未执行”说成“已验证”。

## 3. verified 权限边界

- AI 不能直接把人工审核结论表述为 `verified`
- AI 可以提交建议、审计、风险判断、修复、测试结果
- 涉及 review / verification / approval 的最终结论必须明确区分“系统状态”与“人工确认”

## 4. 文档同步原则

- 优先维护当前有效文档：`../README.md`、`AGENTS.md`、`README.md`、`使用说明.md`、`docs/README.md`
- 历史规划、旧报告统一放入 `docs/archive/`（若 archive 目录已删除，以 git history 为准）
- 当前真实进度以 `README + AGENTS + git history` 为准

## 5. 数据安全原则

未经明确授权，不要做以下操作：

- extraction apply
- 修改 registry / shadow report
- 删除真实数据文件、真实解析产物、真实 artifacts
- 破坏性 git 操作

如任务必须触及上述区域，先说明影响范围，再等待明确确认。

## 6. 修改原则

- 先读再改，不凭印象改
- 先做最小变更，再考虑扩展
- 优先降低误导风险，再追求“文档完整”
- 如果发现文档与当前代码或数据状态冲突，优先修正文档，不要编造“已经完成”的迁移结论

## 7. 常用检查

```bash
cd literature-ai/backend
python -m compileall app findpapers tests
python -m pytest -q
```

如果测试未运行、被跳过、或失败，必须原样说明。
