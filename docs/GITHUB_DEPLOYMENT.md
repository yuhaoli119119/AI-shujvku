# 一个目录修改、上传与发布

2026-10-09 用户明确要求只用 `/opt/AI-shujvku/literature-ai` 一个项目目录。`/opt/literature-ai` 是其别名。本目录是 Git 工作树，GitHub 仓库为 https://github.com/yuhaoli119119/AI-shujvku 。PostgreSQL 是业务数据真源，全部操作在服务器执行。

## 日常操作

在项目目录修改 backend、frontend、prompts 等源码，检查 git status 后只暂存本次改动，提交并 git push origin master。`./update.sh` 无参数时只验证当前版本或生成部署计划，不再隐式 apply；只有 GitHub 可获取的提交能发布，手工修改不会直接覆盖线上代码。

审阅计划后显式运行 `./update.sh apply <完整40位commit> --plan-sha256 <计划摘要>`；只读核验使用 `./update.sh verify <commit>`。apply 不接受分支名、短 SHA 或未精确匹配的 plan。

## 内部运行方式

GitHub 根目录直接包含 backend、frontend、prompts、docker-compose.yml 和 update.sh，不再套一层 literature-ai。发布器在项目内部 releases/<commit>/ 保存该提交的干净检出，代码挂载只读；这是内部部署产物，不是另一个开发工作区。旧冻结提交的嵌套结构继续支持，以便从 GitHub 明确回退。

构建上下文、后端、前端、提示词和网关配置来自 GitHub 提交；仅重建 backend、worker、worker-pdf、owner-gateway、share-gateway、public-gateway 六个服务。PostgreSQL、Redis、MinIO、Grobid 不重建。后端及两个 worker 均须通过自身健康检查；核对服务运行状态、提交标签、只读挂载和 health git_commit 后才写 DEPLOYED_GITHUB_COMMIT 与回执。仍须核验真实域名页面和图表，HTTP 200 不能代替验收。

`verify` 按服务逐项核对候选 release 挂载：backend 的 `/app,/frontend,/prompts`，两个 worker 的 `/app,/prompts`，以及 owner/share/public gateway 的 Nginx 配置模板与 `conf.d` 文件。每项必须是候选 release 中精确路径的只读 bind mount；路径错误、旧 release 来源或可写挂载都会使发布失败。

plan schema v2 在独占锁内记录当前版本的五元快照：pointer SHA、当前 receipt、当前 plan 和当前 compose 的 SHA-256。部署指针必须是精确的 `<40 位小写 commit>+LF` 字节；CRLF、空格、额外空行或其它附加字节会使 plan、apply、verify 和 recover 拒绝继续。apply 全程持有 `<runtime>/deploy-state/deployment.lock`，在 build 前、up 前后、回执写入前后分别复核该快照；另一个进程持有锁时立即拒绝，不能继续使用旧 plan。所有指针写者必须遵守同一锁协议；普通文件只能执行“锁内精确比较后原子替换”，不能容忍绕过锁协议的非协作写者。

apply 使用 `.deployment-transaction.json` 记录 `prepared → built → up → target_verified → committed` 阶段。目标 receipt 先持久化，pointer 最后由锁内精确快照比较后原子替换；比较必须与 plan 的完整五元 expected-current 精确相等，同一 commit 的 receipt 漂移也会拒绝。切换后还须与目标 commit、pointer、receipt、plan 和 compose 的完整五元快照精确相等，目标侧任一对象漂移都不能报告部署成功。提交后再次核验六服务、mount、health、pointer 和 receipt，并在删除事务前重新比对完整目标五元快照；回滚在最终验证后也再次核对旧版本五元快照。up、verify 或 pointer 阶段失败时，部署器尝试用 expected-current 的已审阅 compose 回滚；回滚 pointer 切换同样核对 transaction target 的 commit、pointer、receipt、plan 和 compose，无法精确匹配时不得覆盖其他 writer。`recover` 对指向目标的现场也使用完整五元快照，包括事务中固定的目标 receipt SHA，而不只核对 deployment_id；恢复验证结束后还会再次核对同一快照。恢复失败则保留 `recovery_required`，阻止后续 plan/apply。`verify` 在存在未完成 transaction 时也 fail closed，不再把“容器当前看似正常”误报为部署完成。

中断后先保持现场，再运行显式恢复：

```bash
./update.sh recover <transaction-target-commit>
```

recover 只会把实际 pointer 收敛到 transaction 的 expected-current 或 target。若对应版本、六服务、mount、health、pointer、receipt、plan 和 compose 全部通过验证，则记录 recovery receipt 并清除 transaction；任何不一致继续保留 `recovery_required`，不覆盖未知 pointer。

旧格式 plan 或与当前五元快照不同的 plan 不会被静默覆盖。确需重建同一提交的计划时，必须显式给出 rotation reason 和旧 plan 的精确 SHA-256：

```bash
./update.sh plan <完整commit> \
  --rotate-plan-reason "说明为何需要新 schema 或新回退上下文" \
  --expected-old-plan-sha256 <旧 plan 的 SHA-256>
```

旧 `plan.json` 与其内容寻址的 compose 会原样归档到 `deploy-state/<commit>/plan-history/<old-plan-sha>/` 并写 rotation receipt。新 compose 先写入 `compose.<new-plan-sha>.json`，`plan.json` 最后作为提交点；中断重试只会复用同一旧/新摘要，不改第一次 rotation 时间证据。

禁止对当前 active commit 直接原地 rotation，因为这会改变现行 receipt 引用的 plan。active commit 的紧急回退继续使用保留的父版 helper/plan；常规可验证回退是：候选脚本为父提交生成 schema v2 plan，记录当前候选为 expected-current，审阅 exact plan 后再 apply。

`--runtime` 可以为兼容旧调用保留，但必须 realpath 到唯一生产根 `/opt/AI-shujvku/literature-ai`；任意其它 `/opt/*`、隔离 worktree 或 `/home` 路径均在 fetch、加锁和 plan 写入前拒绝。

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

发布候选的旧成果兼容验收沿用独立验收窗口维护的 preflight 执行包，不在应用仓库复制第二套清单。执行时必须重新固定精确 commit、隔离生产快照、writer 静默窗口、全库/文件指纹、真实 HTTP/MCP 和五项 visuals 证据；模板、默认 dry-run 或合成检查均不能写成兼容 PASS。
