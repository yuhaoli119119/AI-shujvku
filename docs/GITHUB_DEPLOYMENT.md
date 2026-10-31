# 一个目录修改、上传与发布

2026-10-09 用户明确要求只用 `/opt/AI-shujvku/literature-ai` 一个项目目录。`/opt/literature-ai` 是其别名。本目录是 Git 工作树，GitHub 仓库为 https://github.com/yuhaoli119119/AI-shujvku 。PostgreSQL 是业务数据真源，全部操作在服务器执行。

## 日常操作

在项目目录修改 backend、frontend、prompts 等源码，检查 git status 后只暂存本次改动，提交并 git push origin master。用户授权发布时运行 `./update.sh`，脚本自动选择该目录 HEAD、从 GitHub 获取确切提交、生成计划并按计划部署。只有 GitHub 可获取的提交能发布，手工修改不会直接覆盖线上代码。

需要单独核对计划时仍可用 `./update.sh plan <完整40位commit>`、`./update.sh apply <commit> --plan-sha256 <计划摘要>`、`./update.sh verify <commit>`。常规流程无需在多个开发目录间切换。

## 内部运行方式

GitHub 根目录直接包含 backend、frontend、prompts、docker-compose.yml 和 update.sh，不再套一层 literature-ai。发布器在项目内部 releases/<commit>/ 保存该提交的干净检出，代码挂载只读；这是内部部署产物，不是另一个开发工作区。旧冻结提交的嵌套结构继续支持，以便从 GitHub 明确回退。

构建上下文、后端、前端、提示词和网关配置来自 GitHub 提交；仅重建 backend、worker、worker-pdf、owner-gateway、share-gateway、public-gateway 六个服务。PostgreSQL、Redis、MinIO、Grobid 不重建。后端及两个 worker 均须通过自身健康检查；核对服务运行状态、提交标签、只读挂载和 health git_commit 后才写 DEPLOYED_GITHUB_COMMIT 与回执。仍须核验真实域名页面和图表，HTTP 200 不能代替验收。

`verify` 按服务逐项核对候选 release 挂载：backend 的 `/app,/frontend,/prompts`，两个 worker 的 `/app,/prompts`，以及 owner/share/public gateway 的 Nginx 配置模板与 `conf.d` 文件。每项必须是候选 release 中精确路径的只读 bind mount；路径错误、旧 release 来源或可写挂载都会使发布失败。

回执和 `DEPLOYED_GITHUB_COMMIT` 使用同目录临时文件、文件 fsync 和原子替换。apply 在容器重建及 verify 成功后才写回执，回执成功后才推进指针；回执写入失败不会推进指针。已存在的同提交回执可安全原子替换，因此连续发布、回滚到已有回执的版本、再重新发布均可重复执行。

## 服务器内容

data、outputs、deliverables、backend/reports、凭据、.env、.history、releases、deploy-state 和数据库 dump 均由 Git 忽略；不因 Git 操作删除或上传。禁止运行 git clean -fdx 或使用同步删除覆盖这些内容。旧工作区的未提交内容只保留为历史，不从那里开发或部署。

freeze/current-20261009 保留整理前冻结代码；当前服务器数据库冻结检查点不入 Git。目录统一不删除数据库记录、不改文献或图表。

## 维护交接、排空与静默备份

apply 会强制重建 backend 和两个 worker。存在未确认写者时，先由总指挥停止新增派发，并把写者清单作为只读交接单；不得以超时、部署或故障为由释放占用、重放请求、修改 job 状态或删除检查点。

发布前至少完成以下检查，任何一项未满足就不进入 apply：

1. 保存全部 unresolved/potentially-occupying job 的稳定身份、paper、type、status、phase、updated_at 和快照 hash。与各任务负责人确认已停止发新 turn、重试或补写；未知任务继续保持 unknown。
2. 检查 Celery 的 `active`、`reserved`、`scheduled`，两个队列都必须为空。
3. 在只读事务检查 `pg_stat_activity`：不得有 application write transaction，也不得有 idle-in-transaction；不调用可能触发状态回查写库的 processing-status 接口。
4. 连续至少 15 分钟复核 writer 快照 hash、WAL/LSN 和 active write 查询均无业务变化，才算进入静默窗口。
5. 静默窗口内制作一致性 PostgreSQL dump、`/data` 归档、文件 SHA 清单和旧成果 inventory；校验 dump、归档、inventory 以及当前/回滚 release Git bundle。
6. 记录当前 `DEPLOYED_GITHUB_COMMIT`、旧 plan/compose SHA 和候选 plan SHA。若静默期间发生新写入，丢弃本次备份并重新排空、重新采集，不能覆盖旧备份。

可复用只读检查：

```bash
docker exec literature-ai-worker-1 celery -A app.workers.celery_app.celery_app inspect active --json
docker exec literature-ai-worker-1 celery -A app.workers.celery_app.celery_app inspect reserved --json
docker exec literature-ai-worker-1 celery -A app.workers.celery_app.celery_app inspect scheduled --json
docker exec literature-ai-postgres-1 psql -X -U literature_ai -d literature_ai -Atc "BEGIN TRANSACTION READ ONLY; SELECT state,wait_event_type,count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY state,wait_event_type; ROLLBACK;"
```

静默前后都保存 writer 清单。发布或 verify 失败时保留容器日志、回执和指针现场，并用已审阅的旧 commit/plan 回滚应用；不能用整库 dump 覆盖回滚窗口内新增成果。

依赖镜像由 deploy/runtime-image.json 的镜像 ID 与 requirements 哈希固定；Dockerfile.frozen 在固定依赖上清除旧应用源码后复制 GitHub 源码。部署检查固定镜像 ID，依赖升级须在 GitHub 明确更新并验证清单。换服务器需提供精确镜像或验证新的依赖镜像。

## 独立验收授权部署配置（2026-10-26）

同账号的独立窗口复核需要单独的协调者私有 authority。部署通过 `LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE` 显式传递容器内路径，默认空保持关闭；不是工作台密码或会话密钥。经用户授权后，可将新建的独立随机密钥存于既有 `/data` 挂载内受限目录，目录700、文件600，仅实际应用用户和可信协调者可读；密钥不入Git、不进提示词或普通报告。不自动签发任务或恢复旧工作。部署后分别检查配置路径、权限、正式独立复核合同及真实页面，不以存在文件代替业务验收。

## 验收管理工具的独立发布（2026-10-26）

`backend/scripts/prepare_reviewer_access.py` 与 `scripts/reviewer_access.py` 只调用运行服务现有冻结/授权函数及正式HTTP，不修改服务端合同。此类管理工具发布：以当前生产commit为父提交仅收录本次脚本、测试和文档，推送后独立读回远端SHA，并比较实际执行文件与该commit的Git blob哈希；记录工具commit与应用commit。无需仅为脚本重启应用或持久服务，不把管理工具commit写成应用DEPLOYED_GITHUB_COMMIT。若改动app服务/schema/API则仍必须使用上文update.sh正式计划和发布流程。

每次管理调用先在项目私有目录保存目标文献当前快照与现有控制回执，再通过现有服务追加冻结及任务级授权。脚本没有模型调用或任务派发。详情见 [REVIEWER_ACCESS.md](REVIEWER_ACCESS.md)。

## ML Explorer 私有数据资产（2026-10-31）

`frontend/pages/ml_explorer/` 的页面代码进入 GitHub；`app_data.json` 与 `manifest.json` 位于 `**/data/` 忽略范围，不得提交。发布时把两份文件放在项目持久 `data/ml-explorer-releases/<bundle-sha>/`，由 `LITAI_ML_EXPLORER_ASSET_DIR` 指向该目录，并按 `docker-compose.yml` 的只读 bind mount 提供给 backend 的 `/frontend/pages/ml_explorer/data`。

`LITAI_ML_EXPLORER_ASSET_DIR` 对包含 ML Explorer 的提交是必需变量，没有可用的默认目录。变量缺失时 `docker compose config` 会失败；变量已设置但目录不存在、目录名不等于 bundle SHA-256、文件集合、hash/bytes、scope 或 allowlist 不符时，部署 plan 会在任何容器重建前拒绝。这样避免部署出无数据或越界数据页面。回退到旧提交后旧 compose 不引用该变量，变量残留不影响回退。

发布计划必须核对私有 mount 为只读、manifest 内 bundle hash/bytes 与宿主文件一致；未登录 owner 页面和数据请求仍须走 `/login`，公开只读站继续返回 404。不得用 `git add -f` 绕过忽略规则，也不得把 JSON 复制进 `releases/<commit>` 形成 dirty checkout。

首次 plan/apply 必须从包含本次部署校验的候选 checkout 执行 `update.sh`，或先单独发布并核实增强部署辅助脚本；旧生产工作树中的部署器不会因页面提交上线而自动获得这些新增校验。
