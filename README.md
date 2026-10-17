# Literature AI

## 最新更新

### 2026-10-18

实际可用版本以发布回执和页面验收为准。

- 新增：原文覆盖清单可声明正文与补充材料中的对象、材料条件和具体结果对应，区分待处理、未知、未报告与不适用；缺项、过期及具体对应结果可只读查看，按正文/SI和对象名称定位，精确标识可展开；清单不代表科学验收通过。
- 新增：数据批量导入可先预检查且不保存；回执明确部分保存、拒绝、身份待定与冲突，并提供保留行身份的失败项补交建议。
- 修复：编辑数据格时，重输含原文负号或明确科学计数形式的原值，继续保留对应分析数值；原始字符、精度、单位和来源保持不变，无法确定的表达仍保留原文并让数值留空。

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
