# Literature AI

## 最新更新

### 2026-10-25

- 修复：关联补充文献或保存任务派发回执遇到占用冲突时，其他页面仍可响应，保留原任务身份和已存资料。

- 修复：同篇文献写入发生占用冲突时，修正数据身份、保存提取检查和上传文件不会拖住其他页面请求，并保留可重试的冲突提示。

- 修复：原文未印出 Abstract 标题时，可从唯一且版面可核实的灰框摘要取得翻译来源，补存中文摘要；保留已有译文、导读和科学资料，来源不明时继续拒绝。

- 新增：网页 AI 可通过新版文献连接读取正文与 SI、查看真实原页，并保存图表、数据和中文导读；按单篇工作保留已有成果、来源和操作回执。
- 修复：旧文献工具与改造后的资料结构不匹配；新连接使用独立写入权限，并保护同篇任务避免重复写入。

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
