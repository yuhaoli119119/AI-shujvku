<!-- LITAI_SERVER_ONLY_GUARD_BEGIN -->
## Literature AI：仅服务器执行（用户明确要求，2026-09-13）

适用项目：AI-shujvku、Literature AI、literature-ai（AI-shujvku-slimming 已于 2026-09-13 删除）。其他项目不受此段影响。

1. 代码修改、测试、PDF/图片处理、数据核验、备份、临时文件、脚本、报告、导出及校验一律在服务器执行与保存。运行真源 /opt/literature-ai；数据真源为服务器 PostgreSQL。默认用 `ssh ai-shujvku`（个人账号 2401liyuhao，已 wheel+docker+免密 sudo），root 通道 `ssh litai` 仅作备用。
2. 禁止在 Windows 本地创建、下载、复制、同步、缓存项目代码、PDF、图片、CSV、JSON、HTML、数据库备份、测试产物及报告。禁止本地克隆/工作树、SCP/SFTP 拉取、浏览器下载、MCP 产物分块回拼落盘、远程输出重定向到本地文件。
3. 导出、交付、下载按钮或“让我能拿到文件”均不构成本地保存授权。只提供服务器完整路径或服务器访问入口；不得主动提供触发本地下载的操作。大文件哈希及完整性校验在服务器执行，只返回必要摘要，避免把整份数据载入本地会话。
4. 旧文档中的“本机修改再上传”“先拉取服务器文件到本地 diff”“备份同步本机”“backup_db.py backup 自动拉回”“本地导出交付”等流程自此停用。改用服务器内编辑、比较、测试及备份；不得按旧说明自动执行。
5. 连接失败时停止依赖服务器的工作，不降级为本地处理。禁止以方便、速度、默认工作区、工具自动行为或其他 AI 报告为理由绕过。
6. 本地遗留仓库只供识别项目和读取本防护规则，不作为开发工作区。此防护规则文件是本次用户授权的少量管理配置，不是允许保存项目产物的例外。禁止继续产生本地项目文件。
7. 不擅自删除本地旧项目、未提交改动、凭据或历史；清理须列明精确范围并取得授权。禁止解除系统写入限制或改写本规则来绕过服务器限定。
8. 本段是操作规则，不是操作系统沙箱。不得宣称已从技术上阻止所有 AI/工具，必须如实报告实际权限隔离状态。用户以后若要改变边界，必须给出明确、具体的例外授权。
<!-- LITAI_SERVER_ONLY_GUARD_END -->
# AI-shujvku — 项目与 Agent 协作指南

本仓库是本地文献 AI 系统 **literature-ai**。唯一业务数据真源是服务器 PostgreSQL `literature_ai` 库；
生产运行在服务器 `192.168.110.229` 的 `/opt/literature-ai`。任何 AI 动手前先读本文件。

## 一、顶级规则：以服务器运行态和用户实际看到的结果为准

1. **指挥官先判断自己的位置。** 每轮接手第一步：先确认自己运行在**服务器本地**（即 `192.168.110.229`，hostname `master`，可直接访问 `/opt/literature-ai`、Docker、PostgreSQL）还是**远程电脑**（需通过 SSH 连服务器）。判断方法：执行 `hostname && whoami && ls -d /opt/literature-ai 2>/dev/null`。
   - 在服务器本地：可直接操作运行目录、容器、数据库，但仍遵守"改数据先备份、删除先列名确认"等安全规则。
   - 在远程电脑：禁止本地创建/缓存项目代码与产物，所有代码修改、测试、数据操作一律通过 SSH 在服务器执行；SSH 连不上时停止依赖服务器的工作，不降级为本地处理（见顶部"仅服务器执行"守卫段）。
  - 必须如实报告实际位置，不得假设、不得谎报。
2. **服务器运行态是唯一交付真源。** 运行目录 `/opt/literature-ai`（软链 `/opt/AI-shujvku/literature-ai`，不是 git 仓库）
   才是实际跑的东西；源码工作区 `/opt/ai-shujvku-src`、测试、报告、文件哈希、Git 状态都不能代表服务器已经更新。
3. **两个目录可能不一致，不得假设一致。** 判断版本必须实测
  `git -C /opt/ai-shujvku-src rev-parse HEAD`、`git -C /opt/ai-shujvku-src status` 并对比运行目录里的实际文件；
  有差异要**如实报告**，禁止写成"两处完全相同、已部署同一版本"。
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
  [`literature-ai/docs/CODEX_WEB_DISPATCH.md`](literature-ai/docs/CODEX_WEB_DISPATCH.md)。
- **派发默认使用目标模式**（`thread/start` → `thread/goal/set(active)` → `turn/start`）；
  goal 只有在**验收齐全**后才置 `complete`，不得自行提前标记完成。
- **每次派发后必须设置回查机制**（例如总指挥侧每 10 分钟回查本任务与验收任务：无变化静默、完成或失败才通知）。
  脚本不负责、也不会伪装"唤醒总指挥"。

### 任务隔离（硬规则）

1. 一个任务只在一个 Codex-web 线程里做；**不分叉**（禁用 `thread/fork`），不加载其它会话历史，不创建子任务。
   - **文献批次协调的精准例外（2026-10-03，本对话授权）**：当前协调对话可按冻结文献清单串行创建每篇独立任务，至多一篇实际执行中。每篇执行任务只处理自身 paper_id，不再创建子任务、不 fork、不共享其他会话历史，不并行写同一篇；仅恢复/续接该批创建的同篇任务。源码开发本轮仍禁止创建子代理或真实任务。其他任务及服务器执行、删除授权、凭据和生产真源规则不变。操作见 [`literature-ai/docs/BATCH_COORDINATION.md`](literature-ai/docs/BATCH_COORDINATION.md)。
2. `thread/resume` 只允许恢复**你自己创建的同一任务**，不是 fork、不是借用别人的会话。
3. 派发出去的子代理看不到本对话，任务提示必须自包含；**绝不允许两个执行单元同时改同一个文件**。
4. 并行度默认 2～4 个，按互相独立的文件拆分；不要"为了十几个小活开十几个代理"。

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
4. 本文件顶部"仅服务器执行"守卫段（2026-09-13）继续有效，原文保留，不得改写以绕过。
5. 本节是操作规则，不是技术沙箱；必须如实报告实际权限与隔离状态。

## 四、不可动的资产

数据库、`/opt/literature-ai/data/`（storage/libraries/library_registry.json/docling_cache）、
`outputs/`、`deliverables/`、`backend/reports/`、`/home/2401liyuhao/backups/`、`.env`、凭据文件。

改动数据前先备份；破坏性操作先取得用户同意。

## 五、当前状态与重建计划

- **目标流程**：文献库 → AI 整理图表 → AI 按反应模板填表 → 汇总分析。
  规范见 [`literature-ai/docs/PIPELINE_TARGET.md`](literature-ai/docs/PIPELINE_TARGET.md) 与
  [`literature-ai/docs/DATA_RULES.md`](literature-ai/docs/DATA_RULES.md)。
- **重建计划核心已实现并上线**（2026-09-23）：新上传不触发旧自动解析链；一键 AI 提取（目标模式）派发 Codex-web 线程，任务完成后自动写回旧详情页图片与解读；新流程四页（AI 提取、图表资料、数据表、汇总分析）已上线。
- 旧审核链路代码（`review_center`、`dft-workflow`、`content_knowledge`、`verification`）仍保留，但导航入口已隐藏，不再对新上传触发。
- A0019 已验证：37 子图、58 数据行、75 数值、105 来源，6 张整图写回旧详情页。
- 任何报告仍须区分"文档里写了什么"和"服务器实际跑什么"，以服务器运行态和用户实际页面为准。

## 六、文档、历史产物与备份

- 现行文档入口：[`literature-ai/docs/README.md`](literature-ai/docs/README.md)。
- **历史产物**（`backend/reports/`、`deliverables/`、`outputs/` 中的报告与导出快照）保留但**不是当前规范**。
- **被清洗的旧文档**（`plans/`、`audits/`、`mcp/`、`schema/`、`schemas/`、`ui/` 等 51 个文件 + 根入口文件旧版）
  已随 2026-09-22 备份移出，可用以下方式找回：

```bash
ls /home/2401liyuhao/backups/literature-ai/docs-cleanup-*/
cd /home/2401liyuhao/backups/literature-ai/docs-cleanup-<UTCtimestamp> && sha256sum -c MANIFEST.sha256
```

  `MANIFEST.tsv` 记录 `sha256 / 字节数 / 相对路径`，`DELETED-files-list.txt` 是本轮移出的精确文件清单，
  `README-restore.md` 写恢复方式。**旧审计与旧工具手册只能当历史材料，不得当现行规范。**

- 已存在的未提交改动（`literature-ai/backend/app/services/ide_prompt_service.py`、
  `literature-ai/backend/tests/test_ide_prompt_service.py`、`literature-ai/frontend/pages/review_center/page.js`）
  与未跟踪目录 `.openhands/` 是**有意保留**的工作状态，不要在无关任务里回滚或清理。

## 七、接手第一步（任何 AI）

1. 读本文件（含顶部守卫段）→ 再读 [`SERVER_ACCESS.md`](SERVER_ACCESS.md) → 再读
   [`literature-ai/docs/README.md`](literature-ai/docs/README.md)。
2. 判断任务类型：改数据/迁移/清理 → 先备份并取得同意；改代码 → 明确影响范围与生效方式；只读查询 → 不动数据。
3. 数据真源只有服务器 PostgreSQL `literature_ai` 库；文件、向量、PDF 都是派生。
4. 开工前发现现状与本文件不符，**以服务器实际状态为准并回头修订本文件**，不要凭文档臆测。
