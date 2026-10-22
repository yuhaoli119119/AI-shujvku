# Literature AI

## 最新更新

### 2026-10-23

- 修复：中文导读保存后自动核对收尾状态；短暂网络错误可自行恢复，持续失败时可手动重新核对，无需重新生成。

- 优化：文献详情默认打开概览，标题区域更简洁，解析检查与历史记录集中收进可展开的“解析详情”。
- 新增：可单独补充中文摘要与面向科研新人的论文导读，包含研究目标、创新比较、关键证据、结论条件与论证脉络；保留英文摘要并支持切换。
- 优化：中文阅读内容保存后可直接回看，刷新不重复生成；导读未完成可单独重试，不影响已有数据和普通解析。

- 修复：任务实际已运行或结束时可恢复正确状态，避免一直停在准备中；保留原任务与已有结果。
- 新增：选择正文与 SI 后一次启动普通解析，按文件展示原文读取、图表和数据进度；已有成果随时可看。
- 优化：局部文件或字段失败保留其他结果，可继续未完成范围；普通解析无需逐格审批，科学精修与独立审核可稍后进行。
- 修复：重复请求复用原回执，短时断网与限流采用有限重试；任务状态不明时保留原任务并核实，避免重复解析。
- 新增：DOCX 可按原文段落和表格位置读取与引用，无物理页时明确标示。

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
