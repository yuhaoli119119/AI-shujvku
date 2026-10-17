# Literature AI

## 最新更新

### 2026-10-17

处理队列和原生自动停止默认关闭；同账号独立验收授权与原生调度仍需单独接入验收，实际版本以发布回执为准。

- 修复：新整理表格仅有原样文本时，详情页仍可安全查看和复制正文，表号按数字顺序排列（如 S1、S2、S10）；保留跨页来源、原始精度、非数值标记与空缺。
- 新增：图表和数据导入提供可追溯的逐项写入回执，支持相同请求重试核对；裁图失败不覆盖旧图片。
- 新增：提取与独立科学验收分别显示，文件或结果变化后提示重新验收，未复核内容不自动升级为通过。
- 新增：默认关闭并暂停的处理队列、暂停控制及验收待办，空完成或停滞提示人工核对，不自动重派或切换模型。
- 优化：大篇文献工作包明确分页，阅读资料支持按来源版本复用分页文本、页面图像和图表清单。

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
