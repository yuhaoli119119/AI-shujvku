# 一个目录修改、上传与发布

2026-10-09 用户明确要求只用 `/opt/AI-shujvku/literature-ai` 一个项目目录。`/opt/literature-ai` 是其别名。本目录是 Git 工作树，GitHub 仓库为 https://github.com/yuhaoli119119/AI-shujvku 。PostgreSQL 是业务数据真源，全部操作在服务器执行。

## 日常操作

在项目目录修改 backend、frontend、prompts 等源码，检查 git status 后只暂存本次改动，提交并 git push origin master。用户授权发布时运行 `./update.sh`，脚本自动选择该目录 HEAD、从 GitHub 获取确切提交、生成计划并按计划部署。只有 GitHub 可获取的提交能发布，手工修改不会直接覆盖线上代码。

需要单独核对计划时仍可用 `./update.sh plan <完整40位commit>`、`./update.sh apply <commit> --plan-sha256 <计划摘要>`、`./update.sh verify <commit>`。常规流程无需在多个开发目录间切换。

## 内部运行方式

GitHub 根目录直接包含 backend、frontend、prompts、docker-compose.yml 和 update.sh，不再套一层 literature-ai。发布器在项目内部 releases/<commit>/ 保存该提交的干净检出，代码挂载只读；这是内部部署产物，不是另一个开发工作区。旧冻结提交的嵌套结构继续支持，以便从 GitHub 明确回退。

构建上下文、后端、前端、提示词和网关配置来自 GitHub 提交；仅重建 backend、worker、worker-pdf、owner-gateway、share-gateway、public-gateway 六个服务。PostgreSQL、Redis、MinIO、Grobid 不重建。后端及两个 worker 均须通过自身健康检查；核对服务运行状态、提交标签、只读挂载和 health git_commit 后才写 DEPLOYED_GITHUB_COMMIT 与回执。仍须核验真实域名页面和图表，HTTP 200 不能代替验收。

## 服务器内容

data、outputs、deliverables、backend/reports、凭据、.env、.history、releases、deploy-state 和数据库 dump 均由 Git 忽略；不因 Git 操作删除或上传。禁止运行 git clean -fdx 或使用同步删除覆盖这些内容。旧工作区的未提交内容只保留为历史，不从那里开发或部署。

freeze/current-20261009 保留整理前冻结代码；当前服务器数据库冻结检查点不入 Git。目录统一不删除数据库记录、不改文献或图表。

依赖镜像由 deploy/runtime-image.json 的镜像 ID 与 requirements 哈希固定；Dockerfile.frozen 在固定依赖上清除旧应用源码后复制 GitHub 源码。部署检查固定镜像 ID，依赖升级须在 GitHub 明确更新并验证清单。换服务器需提供精确镜像或验证新的依赖镜像。
