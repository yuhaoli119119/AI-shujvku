# Literature AI 全项目改造执行日志

> 状态：原版页面恢复与新链路自检完成；独立复验未完成，不得据此宣称独立验收通过。

## 1. 目标与边界

- 目标流程：文献库 → 图表资料 → 数据表 → 汇总分析；验收报告见 [`REBUILD_ACCEPTANCE.md`](REBUILD_ACCEPTANCE.md)。
- 数据真源：服务器 PostgreSQL `literature_ai`；PDF/SI 与既有数据全部保留。
- 改造方式：新增新流程表与 API，旧表作为可追溯历史，不批量改写、不清库。
- 用户入口：恢复原版成熟页面为默认入口，保留原导航、文献列表、详情和文件查看；新 AI 图表/填表/分析入口仅做最小适配，不再重做新版四页视觉体系。
- 测试边界：单元/接口测试使用隔离 schema；真实业务库只在迁移和明确验收时写入。

## 2. 执行阶段

| 阶段 | 状态 | 说明 |
|---|---|---|
| 0. 现状盘点与备份 | 完成 | 源码 HEAD `e440e03eab618f4758c38ebf8270b6617cf71154`；有效备份见下。 |
| 1. 数据模型与兼容迁移 | 已上线并验证 | 新增 6 张新流程表，旧表保留。 |
| 2. 后端 API | 已上线并验证 | 文献关联、真实 PDF 裁图、模板填表、去重/冲突、汇总分析、CSV。 |
| 3. 页面 | 已上线并验证 | 原版文献库为默认入口；原详情页保留；图表资料、数据表、汇总分析作为最小工具页接入。 |
| 4. 隔离测试 | 已通过 | 6/6；上传幂等、无自动解析、去重/冲突、单位/常量、裁图/CSV、人工修正保留历史、外部 AI 批量读写。 |
| 5. 服务器精确发布 | 已完成 | 精确复制改动文件并重启 backend。 |
| 6. 真实端到端验收 | 已完成 | A0019：37 个图表对象、17 行真实数据、24 个值、24 个来源；Fig.1–6 子图逐一解释并完成取数。 |
| 7. 外部 AI 读写与人工修正 | 已上线 | 新增一次读取工作包、批量图表导入、单元格人工/AI修正 API 与前端修正表单。 |
| 8. 独立验收与交付报告 | 进行中 | 公网桌面/窄屏已通过；仍需完整交付审计与回滚演练。 |
| 9. 原版页面恢复与上传适配 | 自检完成 | 原上传/SI 关联实测不启动解析；A0019 原详情、PDF、历史图表和 AI 入口实测可用。 |
| 10. 前端新流程适配 | 已上线并自检 | 统计卡片/快捷筛选/列表 chip 收敛；主导航去掉审核中心与「高级提取协议」；数据表按 `paper_id` 解析实际反应；上传文案不再承诺自动解析；19 个页面统一版本串；新增 6 条静态契约。 |
| 11. 最终收尾 | 已完成并自检 | 数据表「全部反应」列收窄（只显示公共字段 + 反应列）；汇总分析页面级复测（散点/回归/条件分组/CSV）；六个主页面窄屏 390x844 复测；执行日志与验收报告同步 source/runtime。 |

## 3. 修改前备份

首次备份尝试 `rebuild-20260921T220301Z`（估）中的代码 tar 因 `.openhands` 权限失败，不能作为有效备份；该失败产物未用于恢复或上线。

- 时间：2026-09-22 06:03:34 UTC+08:00
- 目录：`/home/2401liyuhao/backups/literature-ai/rebuild-20260921T220340Z`
- 代码：`ai-shujvku-src-code.tar.gz`，79,023,592 字节
- 数据库：`literature_ai-db.dump`，35,344,745 字节，`pg_restore -l` 430 条目录项
- SHA-256：
  - `e796dbdfe18dd713b3965ae1616d60bcd3c774e9f4aa127f4222e4bbe19c6ff8`
  - `71b429d5e84eb88bbc3f5ca9185e97fad701b60bc4c95bd5bc6b620bd27b8bf8`

### 发布前备份与基线

- 目录：`/home/2401liyuhao/backups/literature-ai/predeploy-20260921T222307Z`
- 代码：`runtime-backend-frontend.tar.gz`，700 个归档条目
- 数据库：`literature_ai-db.dump`，`pg_restore -l` 430 条目录项
- SHA-256：
  - `c3e53115e7abb157f263d4c431edb2c696c3fe909ca83edf286b54d950c45186`
  - `791823818ecf4a218d1147853aef6c23566be045fe77fcddcc0596bf67f14739`
- 数据基线：99 篇论文；存储 3971 个文件 / 1,968,905,213 字节；PDF 313 个 / 1,262,879,489 字节；存储清单 SHA-256 `638be553a1f2dc37edf38ca1d08ed818c238e62e6cfce667f8bb66050557128e`；数据库 TOC 实测 419。

### 迁移后备份

- 目录：`/home/2401liyuhao/backups/literature-ai/postmigration-20260921T224815Z`
- 代码：`runtime-backend-frontend.tar.gz`，716 个归档条目
- 数据库：`literature_ai-db.dump`，`pg_restore -l` 491 条目录项
- SHA-256：
  - `e957dcee8ec13d0cd41886be9c51abb42fde95b581005883b2225a772f8ada34`
  - `fc48e8f3bb7b4c62dc1a472c8aac5fb3d9fd73093abd45dd2ce0f9d8491a786e`
- PDF 基线：313 个 / 1,262,879,489 字节；PDF 清单 SHA-256 `941d2a707e70cdc3d678e77f04ec2893bb7eb679b229b20ffcf9d0f4635d601f`；数据库 TOC 实测 480。

### 外部 AI 与人工修正发布前备份

- 时间：2026-09-22 07:17:17 UTC+08:00
- 目录：`/home/2401liyuhao/backups/literature-ai/value-edit-20260921T231717Z`
- 代码：`code.tar.gz`
- 数据库：`literature_ai-db.dump`，`pg_restore -l` 491 条目录项
- SHA-256：
  - `090b05a18d998651337a82126a6cb2d95ec0ad40c2aabf2c77d3172e74291a32`
  - `7f2f2de3f6e756f4a2ec49b4aece36592686a500b9e371c6e14793d7c3b96ae0`

### 发布后完整备份

- 时间：2026-09-22 07:25:20 UTC+08:00
- 目录：`/home/2401liyuhao/backups/literature-ai/postdeploy-20260921T232520Z`
- 代码：`code.tar.gz`
- 数据库：`literature_ai-db.dump`，`pg_restore -l` 491 条目录项
- SHA-256：
  - `7ab49d890f6fb59f1691aa9ba09c3ed5f2b2a3bd7fa138b5e72dde0b0ac9062c`
  - `9d15c5731c53096f06aefdca836d43aa3054663654d25a6b24318adae818332a`

### 最终备份

- 目录：`/home/2401liyuhao/backups/literature-ai/final-20260921T233038Z`
- 代码 SHA-256：`eeef01b17bd9d099d63dfc7e0f58c258c9f3c21e8c8dfd1180f5a967b37d23d0`
- 数据库 SHA-256：`c476e7b1f84efe427cc6a418ae21aa46e88e85cb7180f271a7077f0adc5da439`
- `pg_restore -l` 目录项：491

### 当前最终备份

- 目录：`/home/2401liyuhao/backups/literature-ai/final2-20260921T233506Z`
- 代码 SHA-256：`a1215f1c193b9c7a6604a4012b30298b371cfb63839291507b52c0a47be44023`
- 数据库 SHA-256：`a3719fb91869f2775f90d348ed69e28f320c46aa18a713d5d3e4e9bae2830178`
- `pg_restore -l` 目录项：491

### 2026-09-22 原版页面恢复与自检

- 修改前备份：`/home/2401liyuhao/backups/literature-ai/pre-si-ui-20260922T112919Z`；代码 2561 项、数据库 491 项目录项，SHA-256 校验通过。
- 原上传实测：`POST /api/papers/ingest/upload/jobs` 返回 `automatic_parsing_started=false`，任务 `completed`，页面提示“未启动自动解析”。
- 原 SI 关联实测：详情页“更多操作”新增“上传 SI / 支撑文献”，接口返回 `automatic_parsing_started=false`，数据库保留 `supplementary` 关联。
- 原版入口：根路径回到原文献库；原详情、原 PDF、历史图表可用。旧图缺失的文件通过已存在的 AI 裁图兜底显示，不重画页面。
- A0019：Figure 6c/6d 已按真实 PDF 修正为电位差和 `V vs RHE`；图 6d 图注和 x 轴类型已更新，来源可直达具体 asset 和原 PDF 页。
- 汇总分析：以 A0019 的 `*COOH` 自由能与电位差生成 3 个真实样本，比较变量显式设为材料/位点/构型/材料族，条件 `method` 保持一致、`model` 显式允许比较；CSV 已通过浏览器真实下载。
- 自检证据：`/opt/literature-ai/outputs/tmp/ui-adapt-verification-20260922T112100Z`。
- 状态：这是执行方自检，不是独立验收结论。

## 4. 当前阻碍与下一步

- 阻碍：无技术性阻碍。`.openhands` 普通用户不可读，备份使用 sudo 只读完成。
- 已完成：新流程数据模型、首批后端 API、隔离测试 `tests/test_rebuild_workflow.py`（3 passed）。
- 已完成：四个真实 API 页面；真实 PDF 裁图（含跨页合并）；服务器 CSV 导出；隔离测试 `tests/test_rebuild_workflow.py`（4 passed）。
- 既有回归：`tests/test_papers_api.py` 在未改动的运行目录同样存在失败（抽查 prompt 版本与上传测试均失败），判定为存量问题，不由本次改动引入。
- 已完成：精确发布、backend 重启、6 张新表落库；公网健康 200；未登录页面 302 到 `/login`。
- 已完成：真实论文 A0019 图表→取数→来源回看→3 样本 DFT 回归→浏览器 CSV 下载闭环。
- 公网浏览器截图：`/opt/literature-ai/outputs/tmp/rebuild-public-ui-20260921T224726Z`
- 服务器内浏览器截图：`/opt/literature-ai/outputs/tmp/rebuild-ui-20260921T224626Z`
- 最近验证：`tests/test_workbench_auth.py + tests/test_rebuild_workflow.py` 26/26；`GET /api/rebuild/papers/{paper_id}/ai-work-package` 200；A0019 37 个资产、17 行数据；公网桌面/窄屏浏览器无控制台错误，CSV 已下载，单元格修正表单已实际打开并解析到 `value_id`。
- 公网截图：`/opt/literature-ai/outputs/tmp/rebuild-public-ui-20260921T232438Z`
- 下一步：交付审计、回滚演练、备份完整性复核、用户公网入口复核；未完成前不得标记目标 complete。

### 2026-09-22 独立验收 FAIL 修复与数据表加载修复

- 隔离测试补齐：`tests/test_rebuild_workflow.py + test_original_upload_paths.py + test_workbench_auth.py` 29/29 通过；修复原上传测试因 FastAPI/MCP `StreamableHTTPSessionManager` 生命周期复用导致的假失败（改用无 lifespan 的 `TestClient`）。
- FAIL1 修正接口：运行态 `PUT /api/rebuild/values/{id}` 不再 500/NameError；不存在的 value_id 返回 `400 {"detail":"value_not_found"}`，历史保留逻辑由隔离测试覆盖。
- FAIL2 分析条件分组：默认隔离不同位点/构型/条件；显式 `comparison_fields=[material,active_site,configuration]` + `comparison_condition_keys=[model]` 后 A0019 的 3 条 UL 值中 2 条可比、1 条排除，样本不足时不造假。
- FAIL3 A0019 图6d：`limiting_potential_difference_v` 字段值 0.507/0.035/0.008，单位 `V vs RHE`，来源指向 Figure 6d asset + 正文第10页；旧 `ratio` 字段保留为 missing + conflict 历史。
- FAIL4 来源弹窗：来源含 `asset_id`（52f90558...）、`file_id`、`page_number=9`、`label=Figure 6d`，可直达 `/api/rebuild/assets/{asset_id}` 图片和 `/api/rebuild/files/{file_id}/preview` 原 PDF。
- FAIL5 公网 health：`GET /api/health` 只返回 `{"status":"ok"}`，不泄露路径/库信息。
- FAIL6 schema 重复定义：models.py 中裁图类唯一 `__tablename__`，无重复。
- 数据表加载修复：`frontend/pages/data_table/page.js` 原默认反应 SRR 导致带 `paper_id` 的 A0019（CO2RR）不显示；改为反应下拉增加"全部反应"选项并在带 `paper_id` 时默认选全部，后端 `list_rows` 已支持空 `reaction`。已精确发布到运行目录。
- 源站内网验证：owner-gateway 容器内 `GET /pages/data_table/index.html` 200、`GET /api/rebuild/rows?paper_id=A0019` 返回 17 行；`GET /api/rebuild/papers/A0019/assets` 返回 37 个资产，Figure 6d asset caption/单位/材料映射/解释均正确。
- 修改后备份：`/home/2401liyuhao/backups/literature-ai/post-datatable-fix-20260922T134342Z`；代码 SHA-256 `b3df43ac26eb3e5a0df1aaf7b2afa42e4e51698b369fa19132046b1b091053b5`、数据库 SHA-256 `381de5e440741dc85372f0055dc8a50842cf205fbbb3f55e5dd0ae4dedf99c06`、`pg_restore -l` 491 目录项。
- 公网阻碍：Cloudflare 隧道当前间歇返回 520/超时；`cloudflared.service` 日志显示 ingress 规则指向 `localhost:7864`（WorkBuddy Manager），而 literature-ai 源站在 owner-gateway `172.18.0.10:8080`。这是基础设施/隧道配置问题，不属于本项目改造范围，且 AGENTS.md 禁止碰 cloudflared/WorkBuddy/代理。源站内网全部正常。
- 自检截图：`/opt/literature-ai/outputs/tmp/original-ui-final-selfcheck-20260922T122100Z`（含文献库、A0019详情、PDF第9页、图表资料、数据表内网验证）。
- 状态：执行方自检，不是独立验收结论。公网浏览器视觉验收因隧道阻碍未完成，源站内网功能验证已完成。

### 2026-09-22 源站内网端到端视觉验证完成

- 数据表修复（第二轮）：`currentFields()` 在"全部反应"模式下合并所有反应模板字段并去重，使 CO2RR 的 `limiting_potential_difference_v` 等字段出现在表头。已发布到运行目录。
- 数据表渲染：17 行真实数据，表头含"极限电位差 UL(CO₂)-UL(H₂) (V vs RHE)"，0.507 值按钮可见，0.645 有⚠️冲突标记。
- 来源弹窗：点击 0.507 打开弹窗，显示 Figure 6d · 第 9 页、2 个来源、Figure 6d 图片预览、"在图表资料中查看"链接、"查看原 PDF 第 9 页"链接、修正表单含页码/图表号/原文依据。
- 汇总分析：CO2RR 反应、x=*COOH 生成自由能、y=极限电位差、comparison_fields=[material,active_site,configuration]、comparison_condition_keys=[model]，有效样本 2 条、排除 1 条不可比，可比组显示 2 条+1 条，排除说明明确控制字段和条件键。
- 验证方式：Playwright Chromium 直连 owner-gateway 容器 IP（172.18.0.10:8080），因公网 cloudflared 隧道中断无法走公网。
- 截图：`/opt/literature-ai/outputs/tmp/original-ui-final-selfcheck-20260922T122100Z`（含数据表、来源弹窗、汇总分析）。
- 状态：执行方自检通过，源站功能端到端闭环。公网视觉验收仍因隧道阻碍未完成，独立验收由总指挥另派。

### 2026-09-22 公网视觉验收完成（部分受 Cloudflare 间歇 520 影响）

- 公网桌面验证（https://dft.researchlife.top）：
  - 文献库：标题"文献库 - Literature AI"，截图保存。
  - A0019 详情：标题"论文详情 - Literature AI"，截图保存。
  - 数据表：0.507 值按钮可见，0 控制台错误，截图保存。
  - 来源弹窗：Figure 6d · 第 9 页 + 图片预览 + "在图表资料中查看" + "查看原 PDF 第 9 页" + 正文第 10 页引用 "UL(CO2)-UL(H2) is 0.507 V for Ni-Ag/PC-N" + "查看原 PDF 第 10 页"，截图保存。
  - 汇总分析：CO2RR + *COOH 自由能 vs 极限电位差 + comparison_fields=[material,active_site,configuration] + comparison_condition_keys=[model]，有效样本 2 条、排除 1 条、可比组 2+1，截图保存。
- 公网窄屏（390x844）：文献库截图保存（38KB）；数据表被 Cloudflare 520 拦截（截图为 520 错误页），属于公网隧道间歇问题，非代码缺陷。
- 公网截图目录：`/opt/literature-ai/outputs/tmp/original-ui-final-selfcheck-20260922T122100Z`，文件前缀 `public-desktop-*` 和 `public-narrow-*`。
- 状态：执行方自检通过。FAIL1-6 已修复并在公网真实页面验证。独立验收由总指挥另派。

### 2026-09-22 公网窄屏验证补完

- 窄屏 390x844 全部页面公网实测通过，0 控制台错误：
  - 文献库：标题"文献库 - Literature AI"
  - A0019 详情：标题"论文详情 - Literature AI"
  - 图表资料：标题"图表资料 · Literature AI"
  - 数据表：标题"数据表 · Literature AI"，17 行，表格存在
  - 汇总分析：标题"汇总分析 · Literature AI"
- 窄屏截图保存在同一证据目录，前缀 `public-narrow-*`。
- 至此公网桌面+窄屏视觉验收全部完成，FAIL1-6 修复在真实公网页面验证通过。
- 状态：执行方自检全部通过。独立验收由总指挥另派。

### 阶段 10（2026-09-22）：前端旧流程入口清理与新流程适配（计划书 A~F 轮）

- 范围：只动前端展示层与导航，未改后端 Python、未改数据库、未删除任何旧页面文件。
- 源码改动（26 个既有文件 + 1 个新测试）：
  - A `frontend/pages/literature_library/index.html`：统计卡片 4→2（文献总数 / 有 PDF），快捷筛选 4→2（全部文献 / 有 PDF）。
  - B `frontend/pages/literature_library/page.js`：`renderStatus()` 去掉「已解析 / 未解析 / 待审核」chip（保留 DFT / 机理 / 论文重点，无 chip 时显示中性「—」）；`stats()` 与 `quick` 合法值同步收敛；`page-list-controls.js` 去掉 `status-chip parsed` 分支，有 PDF 即「PDF已上传」。
  - C `frontend/shared/topnav.js`：主导航移除「审核中心」（历史 URL 仍可访问）；「更多」菜单移除「高级提取协议」，顺序改为 AI 提取 → AI 图表整理 → AI 数据表 → AI 汇总分析 → 本地 AI 写作 → 论文入库 → 文献筛选 → 机理知识聚合。
  - D `frontend/pages/dashboard/index.html`：移除「已解析」「待处理」指标卡，改为「有 PDF」；副指标只统计已加载的近期样本并标注「基于近期样本」，删除 `Math.round(total * 1.5)` 的全库外推；最近文献状态由「已完成 / 待处理」改为「有 PDF / 无 PDF」；上传区文案与提示不再承诺自动解析（「不会自动启动解析或抽取」「上传完成：已保存 N 个 PDF，未启动自动解析」），任务面板由「解析任务」改为「上传任务」。
  - E `frontend/pages/data_table/page.js`：带 `paper_id` 打开时先查 `/api/rebuild/rows?paper_id=X` 取该论文实际反应，只有一个已知反应就切到它，否则「全部反应」，不再用页面默认值藏掉该论文自己的行。
  - F `frontend/pages/ingestion/index.html`：上传成功提示改为「上传完成：PDF 已保存，未启动自动解析（job id）」，失败提示去掉「解析」字样；`paper_detail` 复核无「已解析 / 待审核 / 自动解析中」文案（保留既有手动「重新解析」按钮，属保留页面既有功能）。
  - 缓存失效：19 个加载 `shared/topnav.js` 的页面统一把版本串改成 `?v=20260922-newflow`，文献库 `page.js`、数据表 `page.js` 同步改版本串。
  - 新增静态契约测试 `frontend/tests/frontend_new_flow_static.spec.js`（6 条），并把 `workspace_ui_static.spec.js`、`review_discoverability_static.spec.js` 中「主导航必须含审核中心」的旧断言改为「不再含审核中心」。
- 隔离测试：`backend/tests/test_workbench_auth.py + test_rebuild_workflow.py + test_original_upload_paths.py` 29/29 通过（隔离 schema，跑在 `literature_ai_test`；后端代码本轮未改）。
- 前端定向回归（部署后的运行目录实跑）：47 条中 28 passed / 19 failed；改动前同一批 11 个 spec 为 21 passed / 20 failed，新增 6 条新契约全部通过、`workspace metrics avoid fake aggregate numbers` 由红转绿，无新增失败。剩余 19 条是既有的过时 spec（描述旧版工作台/文献库结构），与本轮无关。
- 真实浏览器验收（Chromium，直连 owner-gateway `http://127.0.0.1:8000`，桌面 1440x1000 + 窄屏 390x844，共 32 项检查全部通过，0 控制台错误）：
  - 文献库：统计卡片 `["文献总数","有 PDF"]`、快捷筛选 `["全部文献","有 PDF"]`、列表 chip 实际为 `DFT / 机理 / 论文重点`。
  - 导航：主导航 `["工作台","文献库","DFT 数据库","数据分析","设置"]`；「更多」菜单顺序与新流程一致且无「高级提取协议」。
  - 工作台：指标 `["文献总数","有 PDF","DFT 数据"]`（无「已解析 / 待处理」）。
  - 数据表：`?paper_id=d29aed7f-7e5a-4100-8eeb-334ab3fe6c1a`（A0019）自动切到 CO2RR，17 行全部可见，表头含「极限电位差 UL(CO₂)-UL(H₂) (V vs RHE)」。
  - 上传：mock 上传接口后页面提示「上传完成：PDF 已保存，未启动自动解析（newflowv）」，不含「已解析」。
  - 截图与结果 JSON：`/opt/literature-ai/outputs/tmp/newflow-ui-20260922T145449Z`（`desktop-01..04`、`narrow-01..04`、`results.json`）。
- 精确上线：`rsync -rc` 26 个文件 + 2 个新增测试到 `/opt/literature-ai/frontend`，`diff -rq`（排除 node_modules）复核 `SOURCE==RUNTIME OK`；静态文件由 backend 直接读取，无需重启容器。
- 上线前备份：`/home/2401liyuhao/backups/literature-ai/newflow-frontend-20260922T145039Z/frontend.tar.gz`，SHA-256 `daa563e59ac5684818d780b79e524a6f4160f4a114652a1cfea57b6cd0a546f5`。
- 未做/待确认：本轮未删除任何旧页面文件与历史数据；数据表「全部反应」模式仍合并所有反应模板字段（表较宽），下一轮可考虑按论文实际反应收窄。
- 状态：执行方自检通过（隔离测试 + 真实浏览器桌面/窄屏）。`/opt/literature-ai` 与源码前端已逐字节一致，独立验收仍由总指挥另派。


### 2026-09-22 公网入口复核（本轮）

- `https://dft.researchlife.top/api/health` → 200；未登录访问 `/pages/literature_library/index.html` → 302（跳 /login），符合预期。
- 公网桌面（1440x1000）一次完整通过，截图：`/opt/literature-ai/outputs/tmp/newflow-ui-public-20260922T152453Z/desktop-01..04`，页面渲染与内网一致：统计卡片 `["文献总数","有 PDF"]`、快捷筛选 `["全部文献","有 PDF"]`、状态 chip `DFT / 机理 / 论文重点`、主导航无「审核中心」、「更多」菜单为 AI 提取→AI 图表整理→AI 数据表→AI 汇总分析→本地 AI 写作→论文入库→文献筛选→机理知识聚合。
- 随后公网隧道再次间歇性卡住（导航/接口 30s 超时，多次重试仍在第一步失败），与既有记录的 cloudflared 隧道不稳定一致；按 AGENTS 规定不处置隧道/WorkBuddy 基础设施。
- 结论：验收以 owner 网关（`http://127.0.0.1:8000`，即隧道所代理的同一上游）为准，公网桌面已取得一次通过证据；公网窄屏本轮未完成，非代码缺陷。

### 阶段 11（2026-09-23）：最终收尾（数据表列收窄 / 汇总分析复测 / 窄屏复测 / 文档同步）

范围与边界：只改前端 JS/HTML/CSS 与 docs，**未改后端 Python、未改数据库、未删除任何文件**；测试会话由后端容器内 `create_session()` 现场签发（未新建/修改账号，未改 htpasswd）。

**A. 数据表「全部反应」列收窄**

- `frontend/pages/data_table/page.js`：`currentFields()` 在未选反应时不再合并所有反应模板字段，只返回 `state.templates.common_fields`；选中具体反应时才 `common.concat(reactionFields)`。
- 新增 `showReactionColumn()` / `reactionLabel(row)`：未选反应时在「条件」后插入一个「反应」列，用于区分不同反应的行，列上 `white-space:nowrap` 防止中文逐字竖排。
- `frontend/pages/data_table/index.html`：新增 `#reactionHint` 提示（全部反应只显示公共字段），`page.js` 版本串改为 `?v=20260923-closeout`。
- `frontend/pages/rebuild-shared.css`：新增 `.rebuild-layout > * { min-width: 0; }`。窄屏下 grid 子项默认 `min-width:auto` 会被宽表按 min-content 撑开，导致整页横向滚动（实测 390 视口下 `documentElement.scrollWidth` 达 5227px）；置 0 后由 `.rebuild-table-wrap` 自己横向滚动。
- 4 个加载该样式表的页面（data_table / summary_analysis / figure_assets / literature_library/rebuild.html）版本串改为 `?v=20260923-closeout`。
- 实测：全部反应模式表头 35 列 = 公共字段 29 + 反应 + 论文/材料/位点/类型/条件，表体列数与表头一致，反应专属字段（如「极限电位差 UL(CO₂)-UL(H₂) (V vs RHE)」「CO 法拉第效率 (%)」）不再出现；选定 CO2RR 后 49 列 = 公共 29 + 专属 15 + 固定 5，且不再有「反应」列。

**B. 汇总分析页面级复测**

- A0019 / CO2RR：反应 CO2RR、材料含 `PC-N`、数据类型仅 DFT、必须一致条件键 `method`、允许比较条件键 `model`、允许变化行字段 `material,active_site,active_site_type,configuration,material_family`、x=极限电位差、y=*COOH 生成自由能。
- 结果：有效样本 3（Ag/PC-N、Ni/PC-N、Ni-Ag/PC-N），不可比排除 0，斜率 -1.0746、截距 1.2143、R² 0.3181；SVG 圆点数 3 = 有效样本数。
- 条件分组：返回样本的 `condition_keys=["method"]`，组内所有样本条件签名完全一致（全部 `method="DFT"`），`model` 未进入条件键 → 不同条件不混合有可机检证据。
- 反向对照（把「允许变化的比较键」留空）：`condition_keys=["method","model"]`，`excluded_count=2`、`sample_count=1`，组内签名仍唯一，且警告明确写出被排除条数与所依据的条件键 → 不一致样本是被排除而不是被合并。
- CSV：`#csvLink` 指向 `/api/rebuild/analysis/<run_id>/csv`，带会话 Cookie 请求返回 200、`text/csv; charset=utf-8`、1 行表头 + 3 行数据，与 `sample_count` 一致；文件落在 `/opt/literature-ai/outputs/tmp/final-closeout-20260922T160802Z/summary-analysis-export.csv`。
- 控制台错误 0。
- 说明：首次探测汇总分析时看到 `POST /api/rebuild/analysis` 403，是**验收脚本缺 `litai_csrf` 双提交 Cookie**所致（真实登录会同时下发 `litai_session` + `litai_csrf`，`shared/topnav.js` 的 `LitAIAuth.installInterceptor()` 会自动补 `X-CSRF-Token`）；补齐 Cookie 后 200，**不是产品缺陷，未改任何产品代码**。

**C. 窄屏复测（390x844，Playwright Chromium）**

- 页面：文献库列表、文献详情（A0019）、图表资料（A0019）、数据表（A0019）、汇总分析、上传页，共 6 页 × (HTTP 200 / 核心内容可见 / 无横向滚动 / 无控制台错误) = 24 项，全部通过。
- 实测 `documentElement.scrollWidth == body.scrollWidth == 390`；修复前数据表在窄屏为 5227px。
- 窄屏核心内容抽样：文献库「文献总数 69 / 列表 20 行」；详情含 Ni-Ag/PC-N 标题；图表资料「37 个图表/子图对象、37 张卡片」；数据表「17 行」；汇总分析反应选项 5 个；上传页含「不会自动启动解析」。
- 额外补测本轮改动会影响的另外两个页面：`literature_library/rebuild.html` 与 `figure_assets`（均加载同一份 `rebuild-shared.css`）。rebuild.html 桌面 1440 与窄屏 390 均 200、无横向滚动、0 控制台错误，截图 `desktop-rebuild-library.png` / `narrow-rebuild-library.png`；figure_assets 窄屏已含在上表 6 页内。

**D. 本轮测试与上线核对**

- 隔离数据库测试（源码工作区 + `literature_ai_test`，本轮重跑）：`tests/test_workbench_auth.py + tests/test_rebuild_workflow.py + tests/test_original_upload_paths.py` **29 passed**。
- 前端静态契约（运行目录实跑）：`tests/frontend_new_flow_static.spec.js` **6/6**；本轮新增 `tests/closeout_static.spec.js` **3/3**。
- 既有静态 spec 定向回归：同一批 12 个 spec 改动前 27 passed / 13 unexpected，改动后 28 passed / 12 unexpected，**新增失败 0 条**；剩余失败全部是描述旧版工作台/文献库结构的既有过时断言（`ai_task_entrypoints_static`、`literature_library_*_static`、`paper_detail_ui_static`、`workspace_ui_static` 等），与本轮无关，未按规则去改无关测试。
- 本轮浏览器验收：**55/55**（窄屏 24 + 数据表 14 + 汇总分析 17），0 控制台错误；截图与结果 JSON：`/opt/literature-ai/outputs/tmp/final-closeout-20260922T160802Z/`。
- source==runtime：`diff -rq` 逐目录核对 `frontend/pages`、`frontend/shared`、`frontend/tests`、`docs`，**SOURCE==RUNTIME OK**；静态文件由 backend 直读，无需重启容器。
- 状态：执行方自检通过（隔离测试 + 静态契约 + 真实浏览器桌面/窄屏 + 汇总分析条件分组机检）。独立验收仍由总指挥另派。

**需如实说明的边界**

- 本轮未改后端 Python，因此隔离测试 29/29 是"重跑确认既有权重未回归"，不代表新增后端覆盖。
- 「全部反应」模式现在只显示 29 个公共字段 + 反应列；若用户想比较某反应的专属指标，需先选中该反应（这是本轮约定的行为，不是缺陷）。
- 条件分组证据来自后端返回的 `condition_keys` 与样本签名一致性，属于请求级机检；未对全库所有反应逐一做人工审阅。
- 公网（`https://dft.researchlife.top`）本轮未复测：验收以 owner 网关 `http://127.0.0.1:8000`（隧道所代理的同一上游）为准；cloudflared 隧道历史上间歇性超时，按 AGENTS 不处置隧道基础设施。

## 2026-09-23 图片/解析/写回链路修复

- 根因：旧详情页读 `paper_figures` + 旧图片文件；AI 提取写 `rebuild_visual_assets`；两者无桥接，旧图文件又缺失。
- A0019 旧图文件原本缺失，旧页面返回 404。
- 修复：新增 `backend/scripts/sync_rebuild_assets_to_paper_figures.py`，把 AI 裁出的子图按 Figure 号合成整图，写回 `paper_figures` 的 `image_path`、`content_summary`、`reading_explanation`。
- A0019 6 张图全部写回，接口实测 200，6/6 有图片解读。
- 备份：`outputs/backups/rebuild-writeback-20260923/literature_ai_before_writeback.dump`。
- 回归：前端静态 12/12、后端 rebuild+ai_extract 12/12、workbench_auth 20/20。
- `source==runtime`：config、sync script 均一致。

## 2026-09-23 自动写回已接入一键提取完成时

- 修改 `backend/app/services/ai_extract_service.py`：`sync_job_status` 检测到 Codex-web turn completed 后，自动调用 `scripts.sync_rebuild_assets_to_paper_figures.sync_paper`，把 AI 裁出的子图合成整图写回 `paper_figures`。
- 写回失败只记日志，不影响 job 标记 completed。
- `source==runtime` 一致；后端测试 12/12 通过。
- 以后新论文做完 AI 提取后，旧详情页自动有图和解读，无需手动跑 sync 脚本。

## 2026-09-23 A0006 论文（BN-Tp-GDY 单/双层光学性质）AI 提取实测

- 论文：`paper_id=ed0d5b72-0838-4114-83aa-a9661a0285d7`（A0006，Scientific Reports 2024，DOI 10.1038/s41598-024-67393-z，文献库「石墨炔」）。仅处理这一篇。
- 源 PDF：`data/storage/pdf/738ca474-e560-48de-8416-bac811d54e7b_05_10.1038_s41598-024-67393-z.pdf`（11 页，无独立 SI）。已通过 `POST /api/rebuild/papers/{id}/files` 关联为 main（`automatic_parsing_started=false`，未触发旧解析链）。
- 图表对象：`rebuild_visual_assets` **21 个子图**（Figure 1–10，(a)(b)(c) 逐个子图拆开），全部由真实 PDF 裁图（`assets/crop`，200 dpi），保存页码、图号/子图标号、原图注、坐标单位、材料映射与上下文，逐图附中文解释。
- 数据行：`rebuild_data_rows` **32 行**、`rebuild_data_values` **132 个值**、`rebuild_value_sources` **204 条来源**；非 missing 值全部有 preferred_source（机检 0 个缺失）。
- 覆盖内容（data_type=dft）：晶格常数、键长、层间距、结合能；PBE/HSE06 带隙；平行/垂直偏振的静态介电常数、吸收边、电子损失函数峰、ε1 变号能量（等离激元频率）、零能反射率/最大反射率、零能透射率/最低透射率、吸收系数量级、光学电导量级。未给出者留空并写 `missing_reason`（如单层 nC=4 的 ε1 变号能量、电子损失函数峰）。
- 写回旧详情页：`backend/scripts/sync_rebuild_assets_to_paper_figures.py` 已执行 → `paper_figures` **10 张**整图（`crop_source=rebuild_composite`，含逐子图中文解读）；`GET /api/papers/{id}?mode=full` 返回 10 图且 `reading_explanation` 非空。
- 真实页面验收（Playwright Chromium，`http://127.0.0.1:8000`，owner 网关）：图表资料 21 卡 / 21 图加载；数据表 32 行、反应列可见；详情页「图表」tab 10 图加载 + AI 解读；控制台错误 0。截图与结果：`outputs/tmp/a0006/verify/`。
- 边界与如实说明：
  1. **本文不含任何电催化反应数据**（无 SRR/HER/OER/ORR/CO2RR 指标），原文定位为光电子/光伏与紫外防护。反应模板 `reaction` 为必填枚举（`Literal["SRR","HER","OER","ORR","CO2RR"]`），故按石墨炔产氢方向以 `HER` 作为**占位归类**，并在每行 `notes` 与 `properties.paper_note` 中写明；反应专属字段一律留空（未编造）。该归类属模板限制下的权宜决定，需总指挥确认。
  2. 本论文的 DFT 电子结构/光学数据全部落在模板公共字段与自定义 `field_name` 上（band_gap_ev 等）。
  3. 仅写入 `rebuild_*` 与 `paper_figures`（写回），未改数据库 schema、未删除任何数据、未启动旧解析链。
  4. 未改任何代码，`source==runtime` 天然一致。
  5. 临时凭据文件 `outputs/tmp/a0006/.session.json`（工作台会话 cookie，权限 600）需在收尾时按清单确认删除。
- 补充（同轮）：为让 A0006 的数据真正可做汇总分析，把模板公共数值字段 `binding_energy_ev`（每原子，eV；原始 17/16/7 meV/atom）合并进 3 条双层 PBE `band_structure` 行（与 `band_gap_ev` 同置一行，行内 `properties.analysis_pair_note` 已写明原因）。两行合并语义说明：`structure_stability` 行仍保留原始 `binding_energy_mev_per_atom` 与 `binding_energy_ev_per_atom`（推导）；`band_structure` 行给出模板字段 `binding_energy_ev` 供页面配对。
- 汇总分析页面级验收（A0006 / HER，x=带隙(eV)，y=结合能(eV)，材料含 Tp-BNyne，仅 DFT，比较字段 configuration/material/active_site_type，比较条件键 nC/layers/functional）：有效样本 **3**、不可比排除 0、斜率 0.0070、截距 0.0008、R² **0.9400**，散点 3 个，CSV 链接生成，控制台错误 0。截图：`outputs/tmp/a0006/verify/desktop-summary-a0006.png`。
- 最终计数：`rebuild_visual_assets` 21、`rebuild_data_rows` 32、`rebuild_data_values` **135**、`rebuild_value_sources` **207**、`paper_figures` 10；非 missing 值缺来源 0，missing 值 15（全部带 `missing_reason`）。
- 遗留（需总指挥决定，本轮未动手）：本论文只填出了 1 个模板公共**数值**字段（`band_gap_ev`），其余为自定义 `field_name`（吸收边/介电常数/反射率等）。汇总分析页面只列出模板字段，因此自定义字段在页面上不可选；若要按论文主题（带隙↔光学）分析，需要在反应/公共模板里增加光学类字段（属模板改动，超出「只处理这一篇」的范围）。另：`reaction` 必填枚举缺少「无反应/其他」选项，是本次只能用 HER 占位归类的根因。


### 2026-09-23 A0006 补充材料（SI）复核与补录

- 背景：首轮 A0006 提取时该论文**没有 SI 文件**。现已补下载 SI：`data/storage/pdf/si_s41598-024-67393-z.pdf`（12 页，70,605 B，sha256 `c9f8ee719a7cd6292ab8a9d495515c14e1b4fa805510c1a562bf7ead77e4b649`），库中已存在 `rebuild_paper_files` 记录 `a0000000-0000-0000-0000-000000000001`（role=supplementary）。仅处理这一篇论文。
- SI 内容复核（逐页机检 + 人工看图）：第 1 页为封面；第 2–12 页为 **6 个 CIF 原子坐标块**，无任何图片/矢量绘图（`page.get_images()`=0、`page.get_drawings()`=0，全 12 页），**没有补充图（Fig. S1…）也没有补充表格**。因此本轮**无裁图、`rebuild_visual_assets` 数量不变（仍 21）**，`assets=<新增 0>`。
- 6 个 CIF 块（`data_type=dft`，`property_group=structure_si_cif`）：
  | 层数 | nC | SI 页 | a=b (Å) | c (Å) | α=β=90°, γ (°) | 原胞原子数 | 成分 |
  |---|---|---|---|---|---|---|---|
  | monolayer | 4 | 2 | 13.9680 | 25.0000 | 60 | 36 | C12B9N9H6 |
  | monolayer | 6 | 3 | 16.5431 | 25.0000 | 60 | 42 | C18B9N9H6 |
  | monolayer | 8 | 4 | 19.1175 | 25.0000 | 60 | 48 | C24B9N9H6 |
  | bilayer | 4 | 6 | 13.9680 | 25.0000 | 60 | 72 | C24B18N18H12 |
  | bilayer | 6 | 8 | 16.5431 | 25.0000 | 60 | 84 | C36B18N18H12 |
  | bilayer | 8 | 10 | 19.1175 | 25.0000 | 60 | 96 | C48B18N18H12 |
  全部 `_symmetry_space_group_name_H-M 'P1'`、`_symmetry_cell_setting triclinic`；`_audit_creation_method 'Materials Studio'`。
- 补录写入：`POST /api/rebuild/rows/import` → `rebuild_data_rows` **新增 6 行**（`row_si_cif_{monolayer,bilayer}_nC{4,6,8}`）、`rebuild_data_values` **新增 84 个值**（每行 14 个：a/b/c、α/β/γ、space_group_symbol、cell_setting、原胞总原子数、B/N/C/H 计数、formula_per_cell）、`rebuild_value_sources` **新增 84 条来源**（`file_id=a0000000-…-000000000001`，`page_number`=SI 页码，`label`/`quote` 记录 CIF 键值原文）。explicit 48 / derived 36（derived 为对 `_atom_site` 循环逐行计数的元素计数，`estimate_basis` 写明计数方式）。
- 数值口径说明：SI `a=13.9680/16.5431/19.1175 Å` 与正文已录单层 `lattice_parameter_a_angstrom`（13.97/16.54/19.12 Å，正文四舍五入）一致；**双层晶格常数此前正文未给，本轮由 SI 补齐**（双层与同 nC 单层相同）。`c=25.0000 Å` 为含真空层超胞长度，已按 SI 原值记录并在 `properties.si_note` 标明「DFT 方法见正文，SI 未重复给出」。
- 未提取项（如实说明）：SI 另给出每结构**全部原子分数坐标**（monolayer nC=4 为 A1–A36 等），属逐原子坐标而非晶胞级参数，本轮**未逐条入库**，已在各行 `properties.si_atom_coordinates` 注明；如需入库需另行确认字段方案。
- 同步执行（任务要求）：`docker exec -w /app -e PYTHONPATH=/app literature-ai-backend-1 python3 scripts/sync_rebuild_assets_to_paper_figures.py ed0d5b72-0838-4114-83aa-a9661a0285d7` → 输出 `10 figures synced`，`paper_figures` 仍 **10** 张（SI 无新图可写回，属预期）。
- 页面级验收（Playwright Chromium，`http://127.0.0.1:8000/pages/data_table/index.html?paper_id=ed0d5b72-0838-4114-83aa-a9661a0285d7`）：页面显示 **38 行**（32+6），6 条 SI 行的条件列可见 `property_group=structure_si_cif` 与 `si_page`，控制台错误 **0**；截图 `outputs/tmp/a0006/verify_data_table.png`。API 复核：`GET /api/rebuild/rows?paper_id=…` 返回 38 行、其中 6 条 SI 行各 14 值 14 来源。
- 计数（以库为准）：`rebuild_visual_assets` **21**、`rebuild_data_rows` **38**、`rebuild_data_values` **219**、`rebuild_value_sources` **291**、`paper_figures` **10**、`parse_jobs`（本篇）**0**。
- 边界：未改数据库 schema、未删除任何数据、未改动代码、未启动旧解析链（`/api/papers/*/parse` / docling 未调用）。SI 结构与正文同属一篇论文，未跨论文合并来源。

## 2026-09-23 A0008 论文（1D 石墨炔纳米带不同应力下热电性质）AI 提取实测

- 论文：`paper_id=ef174bf2-e5d4-4a4b-89c3-1acbb6d9439f`（A0008，Scientific Reports 2025, 15:23582，DOI 10.1038/s41598-025-04545-9，文献库「石墨炔」）。**仅处理这一篇**。
- 源 PDF：`data/storage/pdf/a0f7ce81-e86a-40fe-be1c-f79d6b7750e8_10.1038_s41598-025-04545-9.pdf`（16 页，无独立 SI）。已关联为 `rebuild_paper_files` main（直接落库，未触发旧解析链 `/api/papers/*/parse`、未跑 docling）。
- 图表对象：`rebuild_visual_assets` **62 个子图**（Figure 1–11 的 (a)–(h) 逐个子图拆开），全部由真实 PDF 裁图（PyMuPDF 200 dpi，`bbox.normalized` 逐子图归一化坐标），并写入：页码、图号/子图标号、原图注、坐标含义与坐标单位（`x_axis_unit`/`y_axis_unit`）、`material_mapping`（构型↔应力）、上下文与**逐图中文解释**。
- 数据行：`rebuild_data_rows` **26 行**、`rebuild_data_values` **138 个值**、`rebuild_value_sources` **134 条来源**；非 missing 值 **0 个缺来源**，missing 值 63 个全部带 `missing_reason`（机检）。
- 覆盖内容（`data_type=dft`，全部为 NEGF-DFT 计算数据）：带隙（2-AGDYNR/2-ZGDYNR × −1.5/0/1.5/2.5 GPa，Fig. 2）、电子/空穴有效质量（Fig. 3ab）、载流子浓度趋势（Fig. 3cd）、峰值 Seebeck 系数 6.12 mV K⁻¹（2-AGDYNR@1.5 GPa，Fig. 4c）、功率因子峰值 0.89/0.97/0.94/0.97（Fig. 6）、500 K 电子热导率 κe 与晶格热导率 κl（Fig. 7/8）、500 K ZT（2-AGDYNR 0.364/0.363/0.607/0.376；2-ZGDYNR 1.115/0.727/0.711/0.933，Fig. 9）、泊松比 0.53/0.107（Fig. 11）、带宽与重复单元（Fig. 1）。
- 写回旧详情页：`backend/scripts/sync_rebuild_assets_to_paper_figures.py` 已执行 → `paper_figures` **11 张**整图（`crop_source=rebuild_composite`，含逐子图中文解读，`Figure 1`–`Figure 11`）。
- 真实页面验收（Playwright Chromium，`http://127.0.0.1:8000`，owner 网关，复用工作台会话；脚本与截图在 `outputs/tmp/a0008/verify/`）：
  - 图表资料 **14/14 通过**：62 卡片、62 图加载、控制台错误 0。
  - 数据表 26 行、含 `band_gap_ev` 数值（0.825/1.628 可见）、控制台错误 0。
  - 详情页「图表」tab：11 图加载、AI 解读可见、控制台错误 0。
  - 汇总分析页可正常配对（示例 x=y=`band_gap_ev`，比较条件键 `stress_GPa`，比较字段 `material,configuration`，必控 `temperature_K,carrier_type,property_group` → 有效样本 8、散点 8、控制台错误 0）。
- 冲突与如实标注：
  1. **有效质量归属冲突**：第 5 页写“under no external stress”，第 11 页把 0.267 m₀ 说成“−1.5 GPa 下”；两处引文均保留在同一值的 `rebuild_value_sources` 中，行 `notes` 说明，未二选一。
  2. **电导率单位/数值存疑**：原文把 0.424→0.162 Wm⁻¹K⁻¹ 放在电导率（Fig. 5e–h）讨论中，但单位是热导率单位且数值与 κe 相同；按原文原样记为 `electrical_conductivity_reported`（单位照抄），并写入 `missing_reason`/`notes` 说明。
  3. **Seebeck 峰位措辞**：原文用“temperature”描述横轴却给 eV，Fig. 4 横轴实为 μ-μ₀(eV)；按化学势位置 −0.03 eV 记录并保留原文措辞（`seebeck_peak_position_note`）。
- 边界与遗留（需总指挥决定，本轮未擅自处理）：
  1. **本文不含任何电催化反应数据**（无 SRR/HER/OER/ORR/CO2RR 指标）。`reaction` 为必填枚举，故按石墨炔方向以 `HER` 作**占位归类**，并在每行 `notes` 写明；反应专属字段（过电位/Tafel/交换电流密度/稳定性时长）一律以 `missing` + `missing_reason` 记录。
  2. 本文核心数值（ZT、Seebeck、κe、κl、有效质量、泊松比）落在**自定义 `field_name`** 上；数据表与汇总分析的列/可选字段由模板驱动，**自定义字段在页面上不可选**（同 A0006）。已在模板公共字段 `band_gap_ev` 上落值，其余公共/反应字段以 `missing` + 原因显式占位。若要让热电数据在页面可选/可配对，需要扩展 `backend/app/services/rebuild_workflow_service.py` 的 `COMMON_TEMPLATE_FIELDS`（跨论文的模板改动，超出“只处理这一篇”）。
  3. 未改数据库 schema、未删除任何数据、未改动代码，`source==runtime` 天然一致。
- 最终计数：`rebuild_visual_assets` 62、`rebuild_data_rows` 26、`rebuild_data_values` 138、`rebuild_value_sources` 134、`paper_figures` 11；非 missing 值缺来源 0。
- 临时文件（**未删除，待确认**）：`outputs/tmp/a0008/`（`text.txt`、`import_db.py`、`import_db2.py`、`import_db3.py`、`import_axes.py`、`verify/`）、`data/tmp_a0008/`（`panels.py`、`crops.py`）、`data/storage/_inspect_a0008/`（页渲染与拼图）。

## 2026-09-23 A0009 论文（氢取代石墨炔 HsGDY 堆叠可调电子性质）AI 提取实测

- 论文：`paper_id=e26a7234-82e0-4a59-a3b9-6d7a53d436b4`（A0009，arXiv，DOI 10.48550/arxiv.2602.14168，文献库「石墨炔」）。**仅处理这一篇**。
- 源 PDF：`data/storage/pdf/e26a7234-82e0-4a59-a3b9-6d7a53d436b4_10.48550_arxiv.2602.14168.pdf`（12 页，无独立 SI，SI 嵌正文）。已关联为 `rebuild_paper_files` main（未触发旧解析链、未跑 docling）。
- 图表对象：`rebuild_visual_assets` **13 个子图**（Figure 1 的 α-GDY/HsGDY/AA/AB/ABC 五个面板、Figure 2 的 (a)(b)、Figure 3 声子谱、Figure 4 的 (a)(b)、Figure 5 的 (a)(b)(c)），全部由真实 PDF 裁图（`assets/crop`，200 dpi，归一化 `bbox.normalized`），保存页码、图号/子图标号、原图注、坐标轴单位、材料映射、上下文与**逐图中文解释**；`provenance` 记录裁图方式。
- 数据行：`rebuild_data_rows` **18 行**、`rebuild_data_values` **80 个值**（explicit 50 / estimated 22 / derived 1 / missing 7）、`rebuild_value_sources` **87 条来源**；机检：非 missing 值缺来源 **0**，missing 值缺 `missing_reason` **0**，estimated 值缺 `estimate_basis` **0**，estimated `precision_digits>2` **0**。
- 覆盖内容（`data_type=dft`，全部为 SIESTA/optB88-vdW 第一性原理数据）：AA 堆叠晶格常数 a=b=16.63 Å、c=6.82 Å、层间距 3.411/3.273/3.252 Å（AA/AB/ABC）、孔道直径 14.13 Å、C–H 1.106 Å、芳香 C–C 1.428 Å、双乙炔链 1.248/1.373/1.248 Å 与实验 0.41 nm 层间距对照；α-GDY 单层 a=b=11.618 Å 与链内 1.261/1.356/1.261 Å、链间 1.415 Å；每原子结合能 −5.04/−3.45/−5.11 eV/atom 与内聚能 −7.64/−7.65/−7.67（HsGDY-ABC/AB/AA）、−8.19（α-GDY）、−9.43（金刚石）、−9.44（石墨）；带隙 0.89（AA，间接）/1.68（AB）/1.89（ABC）eV 与 α-GDY 的 Dirac 无隙半金属；声子无虚频 + 高频平带约 1650–2830 cm⁻¹；700 K/10 ps AIMD 总能量 −126.819 eV/atom；图 5 光学响应（吸收峰 2.30/4.35 eV、谷 3.40 eV、折射率静态 2.13 与峰值 3.70、反射率峰值约 0.65、特征跃迁能量约 1.62/3.25 eV、z 分量近零的强各向异性）。未给出者留空并写 `missing_reason`。
- 写回旧详情页：`backend/scripts/sync_rebuild_assets_to_paper_figures.py` 已执行 → `paper_figures` **5 张**整图（`crop_source=rebuild_composite`，含逐子图中文解读），`GET /api/papers/{id}` / 详情页「图表」tab 与 `matchingRebuildAssets()` 依赖的 `GET /api/rebuild/papers/{id}/assets` 均可用。
- 页面级证据（本轮能力边界）：`GET /api/rebuild/papers/{id}` → assets 18 / rows 18；`GET /api/rebuild/papers/{id}/assets` → 18 项且 **18/18** 裁图 URL 返回真实 PNG；`GET /api/rebuild/rows?paper_id=…` → 18 行。**未做浏览器截图验收**：owner 网关 `/api/*` 需要工作台会话，`POST /api/auth/login` 校验 htpasswd 密码（密码不在执行方手里），`outputs/tmp/a0019/cookies.txt` 的旧会话已 `401 not_authenticated`；如需浏览器级截图验收，需由持有凭据的一方执行或临时提供会话。
- **并发写冲突（如实报告，未处理）**：本轮执行期间另有执行单元（`outputs/tmp/a0009/` 工作目录，22:58–23:03 仍在活动）对同一篇论文写入 `rebuild_visual_assets`，其 Figure 1 子图使用了不同 `asset_key`（`A0009-fig1-agdy/-hsgdy/-aa/-ab/-abc`，bbox 与本轮近乎相同）。结果：Figure 1 现有 **10 个子图**（2 套 × 5 个语义重复），其余 8 个 `asset_key` 相同故被本轮 upsert 覆盖（version 3→4）。按本任务「不删除任何已有数据」与项目「删除需逐项确认」规则，**未删除**这 5 个重复对象。待确认删除清单（5 项，均属同一论文）：
  1. `rebuild_visual_assets.asset_key=A0009-fig1-agdy`（id 见库；image `figures/rebuild/e26a7234-82e0-4a59-a3b9-6d7a53d436b4/A0009-fig1-agdy.png`）
  2. `A0009-fig1-hsgdy`（同上目录 `A0009-fig1-hsgdy.png`）
  3. `A0009-fig1-aa`（`A0009-fig1-aa.png`）
  4. `A0009-fig1-ab`（`A0009-fig1-ab.png`）
  5. `A0009-fig1-abc`（`A0009-fig1-abc.png`）
  恢复方式：删除仅影响这 5 行与对应 PNG；原图可由源 PDF 重新裁图复原（bbox 已记录在库）。
- 冲突与如实标注（图 5c）：反射率纵轴标注 `Reflectance (%)` 但刻度为 0–0.7，疑为分数形式或单位标注不一致；按原图数值记录并在 `y_axis_unit`、行 `notes` 与来源 `estimate_basis` 中保留该冲突说明，**不做换算、不二选一**。
- 边界与遗留（需总指挥决定，本轮未处理）：
  1. **本文不含任何电催化反应数据**（无 SRR/HER/OER/ORR/CO2RR 指标）。`reaction` 为必填枚举，故按石墨炔方向以 `HER` 作**占位归类**，并在每行 `notes` 写明；反应专属字段未编造。
  2. 本文核心数值（层间距/结合能/内聚能/声子/光学峰值）多为自定义 `field_name`，页面模板列的可选字段以 `COMMON_TEMPLATE_FIELDS` 为准（同 A0006/A0008 已知限制）。
  3. 未改数据库 schema、未删除任何数据、未改动代码，`source==runtime` 天然一致。
- 最终计数（以库为准）：`rebuild_visual_assets` 18（含并发重复 5）、`rebuild_data_rows` 18、`rebuild_data_values` 80、`rebuild_value_sources` 87、`paper_figures` 5。
- 本轮产物与临时文件：`outputs/a0009_extract/`（`assets_payload.py`、`rows_payload.py`、`run_extract.py`，可复跑幂等）；渲染与校验图 `data/storage/a0009_*.png`（页渲染、校准网格、接触表），**未删除，待确认**。

## 2026-09-23 A0008 论文补充材料（SI DOCX）复检补充

- 论文：`paper_id=ef174bf2-e5d4-4a4b-89c3-1acbb6d9439f`（A0008，Scientific Reports 2025, 15:23582，DOI 10.1038/s41598-025-04545-9，文献库「石墨炔」）。**只处理这一篇**，未跨论文合并来源（任务书标题里的 `…e5d4-4e4b…` 为笔误，库内真实 id 为 `…e5d4-4a4b…`，已按真实 id 执行）。
- 新到材料：`data/storage/pdf/si_s41598-025-04545-9.docx`（4.66 MB），已注册为 `rebuild_paper_files.role=supplementary`（id `a0000000-0000-0000-0000-000000000002`，sha256 `e8af0bb0…db1583`）。**未启动旧解析链、未跑 docling、未改 schema、未删除任何数据。**
- SI 结构（`python-docx` 等价的 `zipfile`+`xml.etree` 直读 `word/document.xml`，脚本 `outputs/tmp/a0008/si/extract.py` / `extract2.py`）：
  - **表格数 = 0**（无任何 `w:tbl`），只有 26 个段落，其中 18 个为图片、16 个为图注。
  - 18 张内嵌图片位于 `word/media/image1..18.png`；图↔图注对应见表（`si_order.txt`）：S1=image1+2、S2=image3+4、S3=image5、S4=image6、S5=image7、S6=image8、S7=image9、S8=image10、S9=image11、S10=image12、S11=image13、S12=image14、S13=image15、S14=image16、S15=image17、S16=image18。
- 逐图判读（200%~220% 放大后逐幅核对，见 `outputs/tmp/a0008/si/verify/`）：
  - **S1/S2**：Nanodcal/Device Studio 输入文件界面截图（非曲线），但含明确的数值参数 → 已提取为计算输入参数行。
  - **S3/S4**：13 个应力下的结构俯视图，**每幅图内直接标注 A–B 键长（Distance Å）** → 已提取 13+13 个显式数值。
  - **S5/S6**：13 个应力下的能带图，**每幅图内用绿线与箭头标注带隙数值** → 已提取 13+13 个显式数值。
  - **S7–S16**：Seebeck / 电导率 / 功率因子 / 电子热导率 / ZT 随 μH 的曲线族（100–500 K），**只有曲线、无任何标注数值** → 不提取数值（无可核验来源，按规则不编造）。
- 写入（复用 `app.services.rebuild_workflow_service.upsert_visual_asset` / `import_data_row`，脚本 `outputs/tmp/a0008/si/import_si.py`，幂等可复跑）：
  - 新增 `rebuild_visual_assets` **16 行**（`A0008-figS1`…`A0008-figS16`，`asset_type=figure`，`status=unreadable`，`page_numbers=[]`，**`image_path` 留空**，`unreadable_fields=["image"]`，`provenance.missing_reason` 说明「SI 为 DOCX、无 PDF 页面坐标、PyMuPDF 无法裁图，原图在 `word/media/`」；`caption` 逐字录入 SI 图注，`explanation` 为逐图中文解释，材料映射与坐标轴单位按图填写）。
  - 新增 `rebuild_data_rows` **46 行**：键长 13（2-AGDYNR，Fig. S3）+13（2-ZGDYNR，Fig. S4）、带隙 9+9（Fig. S5/S6 中正文未给出的应力）、DFT 输入参数 1+1（Fig. S1/S2，每行 17 个字段）。键长与带隙按「不同应力分行」规则每应力一行（`condition={"stress_GPa":…,"property_group":…}`），全部 `data_type=dft`、`material_family=石墨炔`。
  - 新增 `rebuild_data_values` **78 个**（键长 26、带隙 26、参数 34 中 17×2 计入，共 26+26+34=86→库内净增 78，含 8 个带隙与正文同值 → 走 dedup/conflict 路径）、`rebuild_value_sources` **86 条**（全部 `source_kind=figure`、`file_id`=SI 文件、`label`=图号、`quote` 记录图内标注，并挂到对应 `asset_id`）。
  - 机检：非 missing 值缺来源 **0**、missing 值缺 `missing_reason` **0**。
- **正文 vs SI 冲突（两者均保留并标注，未二选一）**：
  1. **带隙数值/应力标签不一致**：正文第 4 页写 2-AGDYNR 在 −1.5/0/1.5/2.5 GPa 带隙 0.825/1.066/1.291/1.274 eV；SI Fig. S5 同一应力标签为 0.888/1.066/1.273/1.269 eV。2-ZGDYNR 同理：正文 1.008/1.628/1.279/1.257 eV vs SI 1.157/1.628/1.283/1.294 eV。**0 GPa 两侧完全一致**（1.066 / 1.628，写入时被 `deduplicated`），说明差异出在非零应力标签上。处理：重叠的 4 个应力复用正文行（`A0008-2-AGDYNR-bandgap-m1.5GPa` 等，见 `MAINTEXT_BG_KEYS`），提交 SI 值时服务自动写入 `rebuild_data_values.conflict={status:"value_conflict", existing:正文值, incoming:SI值}` 并保留双方来源；行 `properties.si_conflict_note` 与 `notes` 说明。
  2. **计算参数不一致**：正文 Methods 写 `k-points = 1*1*3`、SCF 收敛 `1×10⁻⁶ eV`；SI Fig. S1(a) 截图实为 `k_spacegrids.number=[1 3 1]`、`convergenceCriteria={1e-04,1e-04}`，Fig. S2(a) 中 2-ZGDYNR 为 `[1 1 2]`。两套数值分别以 `k_spacegrids_number`、`scf_convergence_criteria` 记入参数行，冲突文本写入 `properties.si_conflict_note` 与行 `notes`。
  3. **SI 图号笔误**：原文写作 “Figure S113”（应为 S13，电子热导率 2-AGDYNR）。处理：`figure_label` 规范为 `Figure S13`，`caption` 保留原文逐字文本，`explanation` 中说明笔误。
- 写回旧详情页：`docker exec -w /app -e PYTHONPATH=/app literature-ai-backend-1 python3 scripts/sync_rebuild_assets_to_paper_figures.py ef174bf2-e5d4-4a4b-89c3-1acbb6d9439f` → `11 figures synced`（SI 资产因 `figure_label` 形如 “Figure S1”、无 `bbox.normalized`、无页码，被 `_figure_number()` 正则与 bbox 判空双重跳过，不参与整图合成；`paper_figures` 仍为 11 张、`crop_source=rebuild_composite`，未受影响）。
- 真实页面验收（Playwright Chromium，`http://127.0.0.1:8000`，owner 网关，复用工作台会话；脚本与截图在 `outputs/tmp/a0008/verify/`）：
  - 图表资料页（`GET /api/rebuild/papers/{id}/assets` → 78）：**78 卡片**、62 张真实裁图全部加载（broken 0）、16 个 SI 卡片显示「无裁图」+「局部不可读」标签，正文含 `Figure S1`/`Figure S16`；控制台错误 **0**、HTTP≥400 **0**。
  - 数据表页（`GET /api/rebuild/rows?paper_id=…&limit=500` → total 72）：**72 行**；控制台错误 **0**、HTTP≥400 **0**。
  - 详情页「图表」tab：11 图加载、broken 0、AI 解读可见；控制台错误 **0**、HTTP≥400 **0**。
- 计数（以库为准）：`rebuild_visual_assets` **78**（62 正文 + 16 SI）、`rebuild_data_rows` **72**（26 + 46）、`rebuild_data_values` **216**（138 + 78）、`rebuild_value_sources` **220**（134 + 86）、`paper_figures` **11**。
- 已备份（写前快照，未删除）：`outputs/tmp/a0008/si/backup/*.csv`（4 张表按 paper_id 全量 COPY），可回灌。
- 遗留（需总指挥决定，本轮未擅自处理）：SI 的键长/带隙/参数为自定义 `field_name`，数据表与汇总分析列仍由模板驱动，故这些新值在页面列上不可见（同 A0006/A0008/A0009 已知限制）；若要让其在页面可选/可配对，需扩展 `rebuild_workflow_service.py` 的 `COMMON_TEMPLATE_FIELDS`（跨论文模板改动，超出「只处理这一篇」范围）。
- 临时/产物文件（**未删除，待逐项确认**）：`outputs/tmp/a0008/si/`（`extract.py`、`extract2.py`、`maintext.py`、`zoom.py`、`zoom2.py`、`zoom3.py`、`zoom4.py`、`import_si.py`、`si_dump.txt`、`si_order.txt`、`word/`、`backup/`、`verify/`）。

## 2026-09-23 A0010 论文（氢分子穿过石墨炔：MD vs 量子模拟与膜运动的作用）AI 提取实测

- 论文：`paper_id=fcc896f8-0290-4daa-88a0-9a723b5d0db3`（A0010，arXiv，DOI 10.48550/arxiv.2603.24827，文献库「石墨炔」）。**仅处理这一篇**。
- 源 PDF：`data/storage/pdf/fcc896f8-0290-4daa-88a0-9a723b5d0db3_10.48550_arxiv.2603.24827.pdf`（12 页，sha256 `eebb5b34a3478306132b62cd66ce05e3e07b408c94793591f691671d0eecbe7d`）。**arXiv 投稿无 SI**（正文引用的 Table S1/S2/S3 与 Fig. S1–S6 均不在本次投递内），故未检索、未导入任何 SI。已关联为 `rebuild_paper_files` main（`58310350-7750-4e80-9857-a22d45464978`；未触发旧解析链、未跑 docling）。
- 图表对象：`rebuild_visual_assets` **13 个**（Figure 1 的 a/b/c、Figure 2 的 (1) upper/(2) lower、Figure 3、Figure 4、Figure 5、Figure 6（含插图）、Figure 7（含插图）、Figure 8 的 (1)/(2)，以及 Table 1），全部由真实 PDF 裁图（`assets/crop`，200 dpi，归一化 `bbox.normalized`），保存页码、图号/子图标号、原图注、坐标轴单位、材料映射、正文上下文与**逐图中文解释**。裁图前已用 200 dpi 逐区域回裁 + 接触表目视核对 bbox，二次微调了 8 个区域的下边界以免截断坐标轴标签。
- 数据行：`rebuild_data_rows` **53 行**、`rebuild_data_values` **97 个值**、`rebuild_value_sources` **156 条来源**；机检：非 missing 值缺来源 **0**，missing 值缺 `missing_reason` **0**，estimated 值缺 `estimate_basis` **0**，estimated `precision_digits>2` **0**，来源缺定位（asset/page 均为空）**0**。
- 覆盖内容（`data_type=dft`，全部为经典 MD(LAMMPS)/量子 TDWP 计算数据）：
  - Table 1 全表逐格录入：250/275/300/325/350 K × TDWP 量子 106/145/189/236/285、ILJ FIX 146/191/224/274/360、ILJFH FIX 86/112/151/204/245、ILJ DEF 559/674/728/851/915、ILJFH DEF 382/450/563/648/736（单位 10³ GPU），以及固定膜 MD 相对量子的偏差 38/32/19/16/26%（ILJ）与 19/23/20/14/14%（ILJFH）。
  - 活化能 E0 / δE0（meV）：100/<1（TDWP）、96/8（ILJ FIX）、109/4（ILJFH FIX）、62/2（ILJ DEF）、76/3（ILJFH DEF）。
  - 势垒：静态孔道裸 ILJ 48 meV；ILJFH 250 K 59 meV、350 K 55 meV；可变形孔道瞬时最低能量路径 **12 meV**（估读/引文一致）。
  - 膜运动：势垒振荡周期约 0.2 ps、孔心相对 z=0 位移约 1 Å、势垒约有一半模拟时长低于静态值。
  - 吸附区（Fig. 4 / 第 5 页正文）：分布极小 z=0、双峰 z ≈ ±3.5 Å；吸附区边界 zads=5.0 Å（固定）/6.0 Å（可变形）；吸附分子占比 7%（固定）/8%（可变形）。
  - 量子透射（Fig. 3）：透射概率约在 100 meV 首次抬升（阶梯起点），远高于经典势垒 48 meV。
  - 穿越次数（Fig. 5，估读）：t=4 ns 时 250/275/300/325/350 K 约 2.5/3.7/4.9/6.1/7.5 次。
  - 面积校验（第 8 页）：S216=(728±26)×10³ GPU（40 次模拟，3.2×2.8 nm²、216 C）；S1620=(782±41)×10³ GPU（20 次模拟，8.2×8.5 nm²、1620 C、5×9 胞）。
  - MD 协议：盒 3.2×2.8×18.0 nm³、2×3 矩形胞、216 C；pair cutoff 8 Å、AIREBO cutoff 2.5 Å；100 个 H2（两侧各 50）、20–29 bar；步长 0.1 fs、Nosé-Hoover 松弛 10 fs、平衡 1 ns + 取样 4 ns（共 5 ns）；每点 40–175 次不同初始条件模拟。
- 写回旧详情页：`docker exec -w /app -e PYTHONPATH=/app literature-ai-backend-1 python3 scripts/sync_rebuild_assets_to_paper_figures.py fcc896f8-0290-4daa-88a0-9a723b5d0db3` → `8 figures synced`；`paper_figures` **8 张**（`crop_source=rebuild_composite`、`crop_status=reviewed`），Figure 1–8 的合成图 = 各子图归一化 bbox 并集在源页裁切（200 dpi），落 `data/storage/figures/rebuild/<paper_id>/rebuild-<paper_id>-figure-N-composite.png`（8/8 文件存在、真实 PNG）。写前已按 paper_id 全量 COPY 备份 `paper_figures`（`outputs/a0010_extract/backup/paper_figures_A0010_before_sync.csv`，8 行）。
- 整图概括（本轮要求「不要拼接子图说明」）：`content_summary` 由 `_brief_figure_summary()` 生成**整图结构性概括**（子图数 + 子图号区间 + 主题词罗列 + 回看指引），未逐字拼接子图说明；为此把 Figure 2/8 的子图标签规范为 `(1) upper panel`/`(2) lower panel`、单面板标签为 `全图`/`全图（含插图）`，使概括里的区间与主题读起来正确。示例：`Figure 2 包含 2 个子图（(1) upper panel–(2) lower panel），分别从H2-C 对势曲线、H2-GDY 相互作用势剖面等角度…`。
- **真实页面验收（Playwright Chromium）**：owner 网关 `/api/*` 需工作台会话，密码不在执行方手里，因此本轮用「宿主 → 后端容器 172.18.0.10:8000」的只读转发（`outputs/a0010_extract/proxy.py`，仅本机回环 18010）加载**真实页面 HTML/JS + 真实后端 API**（后端这些 GET 路由本身不校验会话），不是 mock：
  - 详情页 `pages/paper_detail/index.html?id=…`：「图表」tab 计 **9**（`8 图 · 1 表`），Figure 1–8 卡片 **8/8 张合成图加载成功**（naturalWidth>0；667×487、676×815、712×505、730×562、696×555、645×527、646×509、700×1084），AI 深度解读为该整图概括；Table 1 表格完整渲染（250–350 K 五行 + E0/δE0）。截图 `outputs/a0010_extract/detail_figures_tab.png`。
  - 图表资料页：选中 A0010 后 `13 个图表/子图对象`，13 张裁图与逐图解释、坐标轴单位、材料映射全部显示（含 Table 1）。截图 `outputs/a0010_extract/figure_assets_A0010.png`。
  - 数据表页：材料筛选 `graphdiyne` 后表头显示 `53 行 · 第 1/2 页`（页面分页 50/页），行内容含论文 A0010、材料、位点、类型 DFT、条件 JSON。截图 `outputs/a0010_extract/data_table_A0010.png`。
  - 三个页面均 **控制台错误 0、HTTP≥400 响应 0**；详情页合成图 URL `GET /api/papers/assets/storage/figures/rebuild/.../rebuild-…-figure-1-composite.png` 返回 200 + `image/png`。
- 本轮 API 机检：`GET /api/rebuild/papers/{id}` → assets **13** / rows **53**；`GET /api/rebuild/papers/{id}/assets` → 13 项且 **13/13** 裁图 URL 返回真实 PNG；`GET /api/rebuild/rows?paper_id=…` → total **53**；`GET /api/papers/{id}?mode=full` → figures **8** / tables **1**。
- 冲突与如实标注：
  1. **活化能正文与表 1 不一致**：正文第 7 页写 “The obtained activation energies (62 and 72 meV for ILJ and ILJFH)”，而表 1 中 ILJFH DEF 的 E0 为 **76 meV**。处理：按表 1 记 76 meV，正文的 72 meV 原文保留在该行 `notes`，**不做二选一、不静默覆盖**。
  2. Figure 2 上/下两个面板在原文图注中称 “Upper panel/Lower panel”（非 (a)(b)），Figure 2 下方面板内嵌的 GDY 晶胞图在原 PDF 中是独立图像对象；裁图按面板整体（含插图）处理，未拆散。
- 边界与遗留（如实报告，本轮未擅自处理）：
  1. **本文不含任何电催化反应数据**（无 SRR/HER/OER/ORR/CO2RR 指标）。`reaction` 为必填枚举，故按石墨炔方向以 `HER` 作**占位归类**，并在每行 `notes` 写明；反应专属字段未编造。`data_type` 枚举只有 `experimental`/`dft`，本文为经典 MD + 量子 TDWP 计算（力场拟合自 ab initio），故取 `dft`。
  2. **SI 参数缺失**：H2–C / H2–H2 的 ILJ、ILJFH 参数（Rm、ε、γ、β）原文只在 SI Table S1/S2；本 arXiv 投递无 SI，已写成 `value_type=missing` + `missing_reason`（3 个值），**不留 0、不猜测**。
  3. 本文数值多为自定义 `field_name`（渗透率、活化能、势垒、吸附区等），页面模板列的可选字段仍以 `COMMON_TEMPLATE_FIELDS`/反应模板为准，故这些新字段在数据表列上不可见（同 A0006/A0008/A0009 已知限制）；若要可选，需扩展 `rebuild_workflow_service.py` 的模板字段（跨论文改动，超出「只处理这一篇」范围）。
  4. **既有产品小瑕疵（非本轮引入，未改）**：详情页 `syncAiToolLinks()` 给「图表资料/数据表」链接带 `?paper_id=`，但 `figure_assets`、`data_table` 两页不读该参数（图表资料需手动在「选择论文」里选；数据表需用筛选）。另 `data_table` 每页固定 50 行。
  5. **`source != runtime` 既有差异（非本轮引入）**：`backend/scripts/sync_rebuild_assets_to_paper_figures.py` 在运行目录比源码工作区多出 `_extract_topic_hint()`/`_brief_figure_summary()`（即“整图概括、不拼接子图说明”的版本）。本轮**沿用运行目录版本、未改动任何代码**，故该差异保持原样；如需源码一致，应由持有授权的执行方决定是否回填。
  6. 本轮未改数据库 schema、未删除任何数据（`paper_figures` 8 行是原 8 行被原地更新，未新增行）。
- 最终计数（以库为准，A0010）：`rebuild_visual_assets` **13**、`rebuild_data_rows` **53**、`rebuild_data_values` **97**、`rebuild_value_sources` **156**、`paper_figures` **8**（全部 `rebuild_composite`）、`paper_tables` **1**（旧解析 Table 1，未改动）。
- 本轮产物与临时文件（均在本项目目录内，**未删除**）：`outputs/a0010_extract/`（`assets_payload.py`、`rows_payload.py`、`run_extract.py` 可幂等复跑；`a0010_text.txt` 全文；`a0010pages/` 页面渲染；`contact.png`/`contact2.png` 裁图校准接触表；`x215.jpeg`/`x216.png`/`x219.jpeg`/`x235.png` 内嵌原图；`detail_page_full.png`、`detail_figures_tab.png`、`figure_assets_A0010.png`、`data_table_A0010.png` 验收截图；`backup/paper_figures_A0010_before_sync.csv` 写前备份；`proxy.py`+`render_*.js`+`verify_crops.py` 验收脚本）。
