# Literature AI

## 最新更新

### 2026-10-26

- 修复：验收保存直连工作台，减少环境代理干扰；中断结果核实后可按原请求显式恢复一次，避免重复提交。
- 修复：验收结论保存后断网可保留成功回执并单独核对，结果未知时避免重复提交，失败原因可追溯。
- 修复：同账号独立验收可按当前文献版本准备任务级授权并正式保存，避免因过期配置说明中断；未核验范围仍如实显示。
- 新增：文献详情与外部阅读连接可统一查看当前交付、待补内容、保存记录及独立验收，保留原有摘要、导读、图表和来源入口。
- 优化：新增补充文件单列未覆盖范围，原有导读继续可读；文件版本变化只提示相关成果核对。
- 修复：正文与补充材料同条件同单位的明确数值冲突时优先显示正文，保留双方原值、来源和冲突说明。
- 新增：独立验收的问题可绑定具体成果版本，修正后由独立人员复核；重新提交交付不会隐藏仍待处理的问题。

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
