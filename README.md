# Literature AI

## 最新更新

### 2026-10-19

实际可用版本以发布回执和页面验收为准。

- 新增：来源可保留历史状态与变更原因，当前分析仅使用仍有效的证据，历史记录继续可查。
- 修复：章节按原文顺序显示，标题已有编号时不再重复添加，历史章节保留独立查看。
- 优化：DOCX 图表与数据来源区分原件和派生阅读 PDF；页码身份未核实时提示核对并提供原件下载，已核实的阅读页按对应版本安全回看。

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
./update.sh
```

`./update.sh` 从 GitHub 获取当前已提交版本并部署。未上传的代码不会被当作线上版本。服务器数据、PDF、图片、产物、配置和凭据不会上传 GitHub；PostgreSQL 仍是业务数据真源。

前端、后端和提示词直接位于 `frontend/`、`backend/`、`prompts/`，不再套一层 literature-ai 源码目录。`releases/` 与 `deploy-state/` 是脚本管理的内部发布产物，日常无需操作。

先读 [AGENTS.md](AGENTS.md) 与 [服务器接入说明](SERVER_ACCESS.md)。发布规则见 [GitHub 部署说明](docs/GITHUB_DEPLOYMENT.md)，业务文档见 [文档索引](docs/README.md)。

历史工作区如存在，放在 `.history/`，仅保留旧任务和未提交内容，不再作为开发或发布来源。
