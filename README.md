# Literature AI

## 最新更新

### 2026-10-20

- 修复：有原件哈希的历史 PDF 图表和解读证据可安全跳到对应页，原件变化或派生阅读版本不明时仍提示核对。
- 修复：新裁图保留实际裁切区域及跨页顺序，便于对照完整子图、坐标轴和图注。
- 修复：数据表来源按钮核对原件与阅读件版本，DOCX 原件定位不再误作 PDF 物理页跳转。
- 优化：旧原生 PDF 来源可按登记原件跳页，并保留旧页码待核对提示；不把历史来源升级为已审核。

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
