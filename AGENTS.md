# AI-shujvku — 项目与 Agent 协作指南

本仓库是文献 AI 系统 **literature-ai**。唯一业务数据真源是服务器 PostgreSQL `literature_ai` 库；
生产运行在服务器 `192.168.110.229`（hostname `master`）的 `/opt/literature-ai`。用户通过网页远程控制服务器，AI 直接在服务器上执行，不经过用户笔记本。任何 AI 动手前先读本文件。

## 一、顶级规则：以服务器运行态和用户实际看到的结果为准

1. **每轮接手第一步：确认自己在服务器上。** 执行 `hostname && whoami && ls -d /opt/literature-ai`，应得到 `master` / `2401liyuhao` / `/opt/literature-ai`。
   - 在服务器：可直接操作运行目录、容器、数据库，但仍遵守"改数据先备份、删除先列名确认"等安全规则。
   - 不在服务器：如实报告，停止依赖服务器的部分，不在本地开工、不在本地生成项目产物。
   - 必须如实报告实际位置与权限，不得假设、不得谎报。
2. **GitHub 是代码版本与部署来源（用户明确要求，2026-10-09）。** 代码仓库 `yuhaoli119119/AI-shujvku` 的明确 commit 是发布依据。运行目录 `/opt/literature-ai` 是部署与持久数据位置；服务器手工修改不能作为发布版本。业务数据真源仍为 PostgreSQL，服务器执行限定保持有效。唯一开发、提交与部署入口为 `/opt/AI-shujvku/literature-ai`（别名 `/opt/literature-ai`），本目录本身是 Git 工作树。新流程见 `docs/GITHUB_DEPLOYMENT.md`。源码提交、发布回执与真实域名页面分别核验，不能仅凭提交宣称上线。
3. **只在一个项目目录开发（用户明确要求，2026-10-09）。** 在 `/opt/AI-shujvku/literature-ai` 修改代码、Git 提交与上传。禁止再用 `/opt/ai-shujvku-src` 或另建 source 开发目录。`data/`、`outputs/`、`deliverables/`、`.env`、凭据、`.history/`、`releases/` 和 `deploy-state/` 保留在服务器且不提交。网站只运行从 GitHub 指定提交生成的内部只读发布目录；它是部署产物，不是第二个开发项目。核验 `git rev-parse HEAD`、`git status`、`DEPLOYED_GITHUB_COMMIT` 与真实页面，未提交修改不代表已经上线。
4. **用户实际看到的页面是前端验收的最高标准。** 用户说不对就是不对，不得用"本地改了""测试过了""接口 200"反驳。
5. 未完成服务器验证与真实页面验证时，只能报告“已完成本地部分，服务器/用户侧尚未验收”；
   **禁止**使用“已完成”“已闭环”“已交付”。
6. 用户明确表示结果不正确时，先核查服务器运行态、实际响应、缓存与用户入口，停止围绕本地测试辩解。
7. **所有测试、备份、临时文件、产物必须放进项目目录**（如 `/opt/literature-ai/outputs/...` 或 `/opt/literature-ai/...`），
   不得写入 `/home/2401liyuhao` 根目录；如必须生成临时文件，先创建项目内专用目录，任务结束后归档或删除。

## 二、角色与派发（本轮确立）

- **总指挥**：在外部，负责分派任务、验收、决定目标是否完成。额度有限，因此任务应尽量一次派清、让执行方自行持续推进。
- **Codex-web**：在服务器上执行被派发的任务，只做本轮任务范围内的事。
- 派发通道与脚本：`scripts/codex_web_dispatch.cjs`，用法与边界见
  [`docs/CODEX_WEB_DISPATCH.md`](docs/CODEX_WEB_DISPATCH.md)。
- **派发默认使用目标模式**（`thread/start` → `thread/goal/set(active)` → `turn/start`）；
  goal 只有在**验收齐全**后才置 `complete`，不得自行提前标记完成。
- **每次派发后必须设置回查机制**（例如总指挥侧每 10 分钟回查本任务与验收任务：无变化静默、完成或失败才通知）。
  脚本不负责、也不会伪装"唤醒总指挥"。

### 任务隔离（按需开子代理）

1. 子代理按情况开：任务能拆成互相独立的子任务时，可用 `spawn_agent` 并行推进，无需逐次申请；拆不开、或写同一份文件时串行做，不要为了拆而拆。
   - 子代理看不到本对话，任务提示必须自包含。
   - 共用 `paper_id`、同一文件、同一数据行不得并行写；按冻结清单逐篇处理时，可为每篇开独立任务，至多一篇执行中，只处理自身 `paper_id`，仅恢复/续接同批创建的同篇任务。操作见 [`docs/BATCH_COORDINATION.md`](docs/BATCH_COORDINATION.md)。
2. `thread/resume` 只允许恢复**你自己创建的同一任务**，不是 fork、不是借用别人的会话。
3. 并行度默认 2～4 个，按互相独立的文件拆分；不要"为了十几个小活开十几个代理"。

### 权限按具体任务给

- 权限范围以**本任务明确划定的范围**为准，不给"顺手也能改"的默认授权。
- 任务未覆盖的文件/服务/数据，即使看起来有问题，也只报告、不擅自处理。

## 三、安全与删除规则（保留）

1. **敏感删除必须先经用户明确确认。** 包括但不限于：`rm -rf`（任何路径）、删除数据库库/表/记录/字段、
   删除或覆盖数据库备份与 dump、`docker volume rm`、`docker compose down -v`、
   删除 `/opt/literature-ai/data/` 下任何内容（PDF、解析产物、文献库配置、docling 缓存）、
   删除 `storage/`、`outputs/`、`deliverables/`、`artifacts/` 等产物目录、删除 Git 分支/标签/远端引用、
   卸载或停止生产容器。
   - 执行前必须**先逐个文件列出精确路径、数量与影响范围**，说明恢复方式，然后**等待用户明确同意**。
   - 未获同意时只报告"待确认删除清单"，不得以"清理""释放空间""顺手"为由先行删除。
   - 只读查询、新建文件、新增记录不受此限；本条只约束删除与破坏性覆盖。
   - **不使用通配符删除**；必须逐文件列名。
2. **一次性授权不等于永久授权**：2026-09-22 的文档清洗（"项目文件清洗/替换、无需逐项确认"）是用户针对**该轮、该文件清单**
   的明确授权；它**不构成**对后续任何删除的长期授权。之后的删除仍按本节第 1 条逐文件确认。
3. 凭据规则：服务器 root 密码/私钥/API key **不进仓库、不发云端 AI、不贴进对话、不写进会外传的文档**。
   外部 AI 只给 MCP key（L2），绝不给 SSH/服务器权限。
4. 本节是操作规则，不是技术沙箱；必须如实报告实际权限与隔离状态。

## 四、不可动的资产

数据库、`/opt/literature-ai/data/`（storage/libraries/library_registry.json/docling_cache）、
`outputs/`、`deliverables/`、`backend/reports/`、`/home/2401liyuhao/backups/`、`.env`、凭据文件。

改动数据前先备份；破坏性操作先取得用户同意。

## 五、当前状态与重建计划

- **目标流程**：文献库 → AI 整理图表 → AI 按反应模板填表 → 汇总分析。
  规范见 [`docs/PIPELINE_TARGET.md`](docs/PIPELINE_TARGET.md) 与
  [`docs/DATA_RULES.md`](docs/DATA_RULES.md)。
- **重建计划核心已实现并上线**（2026-09-23）：新上传不触发旧自动解析链；一键 AI 提取（目标模式）派发 Codex-web 线程，任务完成后自动写回旧详情页图片与解读；新流程四页（AI 提取、图表资料、数据表、汇总分析）已上线。
- 旧审核链路代码（`review_center`、`dft-workflow`、`content_knowledge`、`verification`）仍保留，但导航入口已隐藏，不再对新上传触发。
- A0019 已验证：37 子图、58 数据行、75 数值、105 来源，6 张整图写回旧详情页。
- 任何报告仍须区分"文档里写了什么"和"服务器实际跑什么"，以服务器运行态和用户实际页面为准。

## 六、文档、历史产物与备份

- 现行文档入口：[`docs/README.md`](docs/README.md)。
- **历史产物**（`backend/reports/`、`deliverables/`、`outputs/` 中的报告与导出快照）保留但**不是当前规范**。
- **被清洗的旧文档**（`plans/`、`audits/`、`mcp/`、`schema/`、`schemas/`、`ui/` 等 51 个文件 + 根入口文件旧版）
  已随 2026-09-22 备份移出，可用以下方式找回：

```bash
ls /home/2401liyuhao/backups/literature-ai/docs-cleanup-*/
cd /home/2401liyuhao/backups/literature-ai/docs-cleanup-<UTCtimestamp> && sha256sum -c MANIFEST.sha256
```

  `MANIFEST.tsv` 记录 `sha256 / 字节数 / 相对路径`，`DELETED-files-list.txt` 是本轮移出的精确文件清单，
  `README-restore.md` 写恢复方式。**旧审计与旧工具手册只能当历史材料，不得当现行规范。**

- 已存在的未提交改动（`backend/app/services/ide_prompt_service.py`、
  `backend/tests/test_ide_prompt_service.py`、`frontend/pages/review_center/page.js`）
  与未跟踪目录 `.openhands/` 是**有意保留**的工作状态，不要在无关任务里回滚或清理。

## 七、接手第一步（任何 AI）

1. 读本文件 → 再读 [`SERVER_ACCESS.md`](SERVER_ACCESS.md) → 再读
   [`docs/README.md`](docs/README.md)。
2. 判断任务类型：改数据/迁移/清理 → 先备份并取得同意；改代码 → 明确影响范围与生效方式；只读查询 → 不动数据。
3. 数据真源只有服务器 PostgreSQL `literature_ai` 库；文件、向量、PDF 都是派生。
4. 开工前发现现状与本文件不符，**以服务器实际状态为准并回头修订本文件**，不要凭文档臆测。
