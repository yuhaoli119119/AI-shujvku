# Literature AI 服务器接入与项目位置

唯一项目目录 `/opt/AI-shujvku/literature-ai`；`/opt/literature-ai` 是指向它的软链。这个目录本身是 Git 仓库，修改代码、提交、上传 GitHub 和部署均在这里完成。

服务器 hostname `master`，个人账号 `2401liyuhao`；业务数据库为服务器 PostgreSQL `literature_ai`。所有开发、测试、文件处理和核验在服务器执行，不拉回本地。远程 SSH 别名由连接方配置，以实际连接结果为准。

GitHub 仓库：https://github.com/yuhaoli119119/AI-shujvku 。

```bash
cd /opt/AI-shujvku/literature-ai
git status
git add <本次代码文件>
git commit -m "修改说明"
git push origin master
./update.sh
./update.sh verify "$(cat DEPLOYED_GITHUB_COMMIT)"
docker compose ps
```

代码修改须提交并上传，再从 GitHub 部署；不能以服务器未提交文件替代发布。`./update.sh` 只更新六个应用和网关服务，不重建数据库、Redis、MinIO、Grobid。

数据库、PDF/图片、.env、密码文件、outputs、deliverables、历史工作区与部署产物留在服务器，不上传 GitHub。数据库变更与删除另按 AGENTS.md 的具体授权执行。本次目录统一不修改业务数据。
