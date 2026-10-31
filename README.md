# Literature AI

## 最新更新

### 2026-11-01

- 修复：DFT 数据的双原子与单原子导出范围仅作用于锂硫库的 SRR 记录，不再全部显示为 0，也不再跨库混入。
- 修复：“结合能”按明确的 Li₂Sₙ 物种及计算条件归一并兼容不同字段词序，排除金属支撑能和相对结合能；证据弹窗仅把已核验来源标为 PDF 页码。
- 兼容：既有 `binding_energy` 分析字段和相关查询继续可读，保留为不进入默认矩阵的旧版兼容视图；新版页面继续提供按物种和条件拆分的数据。
- 修复：DFT CSV 数据范围导出会返回基础、符合和排除数量，零符合场景可明确显示范围排除而不是误报没有探索性记录。
- 优化：筛选变化会取消旧导出响应并清除旧计数；导出按钮明确标为探索 JSON；慢速质量概览提供更充分的等待时间。

[完整更新记录](docs/releases/release-notes.md)

唯一项目目录：`/opt/AI-shujvku/literature-ai`。`/opt/literature-ai` 是同一目录的别名。

这里同时是 Git 仓库、代码修改位置和服务器项目目录。代码仓库为 [yuhaoli119119/AI-shujvku](https://github.com/yuhaoli119119/AI-shujvku)。

修改与发布：

```bash
cd /opt/AI-shujvku/literature-ai
git status
git add <本次修改的代码文件>
git commit -m "说明改了什么"
git push origin master
./update.sh                         # 仅生成部署计划
./update.sh apply <完整commit> --plan-sha256 <已审阅计划摘要>
./update.sh recover <transaction-target-commit>
```

`./update.sh` 从 GitHub 获取当前已提交版本，但无参数时只生成计划。审阅 exact plan、当前版本和回退路径后，再显式运行 apply。未上传的代码不会被当作线上版本。服务器数据、PDF、图片、产物、配置和凭据不会上传 GitHub；PostgreSQL 仍是业务数据真源。

前端、后端和提示词直接位于 `frontend/`、`backend/`、`prompts/`，不再套一层 literature-ai 源码目录。`releases/` 与 `deploy-state/` 是脚本管理的内部发布产物，日常无需操作。

先读 [AGENTS.md](AGENTS.md) 与 [服务器接入说明](SERVER_ACCESS.md)。发布规则见 [GitHub 部署说明](docs/GITHUB_DEPLOYMENT.md)，业务文档见 [文档索引](docs/README.md)。

历史工作区如存在，放在 `.history/`，仅保留旧任务和未提交内容，不再作为开发或发布来源。
