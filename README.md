# Literature AI

## 最新更新

### 2026-10-31

- 新增：锂硫双原子单库“ML 数据探索”页面，可查看数据用途、材料分布、当前目标 readiness、历史 dummy OOF 和 F1/F2 SHAP。
- 优化：当前 RDS 与历史 RDS 分层展示，Li₂S Step I/II/完整路径状态、模型可用性和数据来源可追溯。
- 修复：数据页明确区分统计单元、数据行、训练目标行和模型解释次数，避免把跨域成员或重复模型视图当作独立样本。
- 修复：部署计划记录当前版本与回退资产，apply 使用独占锁、当前与目标完整快照复核、原子替换和失败恢复，避免并发发布造成容器与发布记录不一致。

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
