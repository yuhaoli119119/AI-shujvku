# Literature AI 全项目改造验收报告

> 状态：原版页面恢复、新链路适配与 2026-09-23 收尾复测均通过执行方自检；**独立复验未完成**，本报告只记录执行方已验证事实，不得据此宣称独立验收通过。

## 1. 用户入口与页面

- 公网入口：`https://dft.researchlife.top`
- 服务健康：`https://dft.researchlife.top/api/health` 返回 200
- 默认入口：原版文献库；原导航、文献列表、详情、PDF/SI 和历史图表保留。
- 新工具入口：文献库“AI 提取”、导航“更多”和论文详情“更多操作”中的 AI 图表/数据表/汇总分析链接均带当前 `paper_id`。
- 未登录访问受保护页面会 302 到 `/login`
- 桌面与窄屏页面均无控制台错误
- 最新自检截图与 CSV：`/opt/literature-ai/outputs/tmp/final-closeout-20260922T160802Z`（本轮收尾，含六页面窄屏、数据表、汇总分析、CSV）
- 上一轮新流程适配证据：`/opt/literature-ai/outputs/tmp/newflow-ui-20260922T145449Z`、`/opt/literature-ai/outputs/tmp/ui-adapt-verification-20260922T112100Z`

## 2. A0019 真实闭环

- 论文：`Synergistic engineering of heteronuclear Ni-Ag dual-atom catalysts for high-efficiency CO2 electroreduction with nearly 100% CO selectivity`
- 文献库总量：99 篇，均未清库、未删除
- `paper_id`：`d29aed7f-7e5a-4100-8eeb-334ab3fe6c1a`
- 图表对象：37 个子图（`rebuild_visual_assets`），覆盖 Fig.1–Fig.6 全部子图
- 整图写回：6 张合成整图已写回 `paper_figures`，旧详情页直接可见并带图片解读
- 数据行：58 行（50 行实验、8 行 DFT）
- 数据值：75 个，全部带来源
- 来源记录：105 条
- 汇总回归：已完成 3 个 DFT 配对样本

## 3. 实测能力

1. 正文/SI 上传仅保存关联，不自动启动解析链。
2. 真实 PDF 裁图，支持同一对象跨页合并。
3. 每个子图单独解释，保存页码、图号、图注、坐标单位、材料映射和上下文。
4. 反应模板覆盖 SRR/HER/OER/ORR/CO2RR，包含单/双原子、载体、石墨炔及衍生物、结构电子性质、吸附稳定性、硫中毒等字段。
5. 批量幂等填表，重复结果去重合并来源，明确值优先估读值，不同条件不合并。
6. 冲突保留历史，不静默覆盖，局部问题不阻塞整篇。
7. 汇总分析支持筛选、x/y 数值字段、散点图、回归/相关性、样本量与缺失/排除说明。
8. CSV 由用户主动点击浏览器下载，服务器生成并校验。
9. 单元格可点开真实来源，并支持人工/AI修正，旧值保留在冲突历史。
10. 外部 AI 批量读写工具与提示词见 `docs/AI_WORKBENCH_API.md`。
11. 一键 AI 提取（`POST /api/rebuild/papers/{paper_id}/ai-extract/jobs`）派发 Codex-web 目标模式线程；任务完成后自动写回 `paper_figures`，无需手动跑 sync 脚本。

## 3.1 2026-09-22 自检复现

- 原上传按钮上传 PDF，HTTP 200；数据库任务 `completed`，`automatic_parsing_started=false`，无 queued/running 解析任务。
- 原详情页上传 SI，HTTP 200；数据库保留 `supplementary` 关联，任务 `completed`，`automatic_parsing_started=false`。
- A0019 详情 6 个历史图卡可见；原图文件缺失处使用已有 AI 裁图兜底，并标注“AI 裁图”。
- Figure 6c 保留图值 `0.645` 与正文值 `0.64` 的双方来源和精度差异；Figure 6d 修正为电位差，单位 `V vs RHE`，历史 `ratio` 字段保留为缺失/历史。
- 来源弹窗可直接打开具体 asset 与原 PDF 页；Figure 6c/6d 均实测。
- 汇总分析使用 `*COOH` 自由能与电位差生成 3 个样本；比较变量显式为材料/位点/构型/材料族，条件 `method` 保持一致、`model` 显式允许比较；CSV 浏览器真实下载。

## 3.2 阶段 11（2026-09-23）收尾复测

数据表列收窄：

- 「全部反应」模式不再合并所有反应模板字段，只显示 29 个公共字段 + 一个「反应」列；实测表头 35 列，表体列数与表头一致，反应专属字段（「极限电位差 UL(CO₂)-UL(H₂) (V vs RHE)」「CO 法拉第效率 (%)」等）不再出现。
- 选中 CO2RR 后恢复该反应专属字段：49 列 = 公共 29 + 专属 15 + 固定 5，且不再有「反应」列。
- 带 `paper_id` 的 A0019 仍自动切到 CO2RR，17 行全部可见。

窄屏（390x844，Playwright Chromium）六页面复测：

- 文献库列表 / 文献详情（A0019）/ 图表资料（A0019）/ 数据表（A0019）/ 汇总分析 / 上传页，24 项检查（HTTP 200、核心内容可见、无横向滚动、无控制台错误）全部通过。
- 修复项：共享样式表 `frontend/pages/rebuild-shared.css` 增加 `.rebuild-layout > * { min-width: 0; }`。修复前数据表窄屏整页横向滚动到 5227px，修复后 `documentElement.scrollWidth == body.scrollWidth == 390`。

汇总分析（A0019 / CO2RR）页面级复测：

- 条件：反应 CO2RR、材料含 `PC-N`、数据类型仅 DFT、必须一致条件键 `method`、允许比较条件键 `model`、允许变化行字段 `material,active_site,active_site_type,configuration,material_family`、x=极限电位差、y=*COOH 生成自由能。
- 结果：有效样本 3、不可比排除 0、斜率 -1.0746、截距 1.2143、R² 0.3181；散点图 3 个圆点；无 JS 错误。
- 条件分组不混合（机检）：样本 `condition_keys=["method"]`，3 个样本条件签名一致（均 `method="DFT"`），`model` 未进入条件键。
- 反向对照（允许比较键留空）：`condition_keys=["method","model"]`、`excluded_count=2`、`sample_count=1`，组内签名仍唯一 → 条件不一致的样本被排除而非合并。
- CSV：`/api/rebuild/analysis/<run_id>/csv` 返回 200、`text/csv; charset=utf-8`，1 行表头 + 3 行数据（Ni-Ag/PC-N 0.507→0.645、Ni/PC-N 0.008→0.777、Ag/PC-N 0.035→1.63）。

本报告数据表与窄屏行为的边界：

- 「全部反应」只显示公共字段是 2026-09-23 约定的行为：要看某反应专属指标需先选中该反应。
- 本轮只改前端 JS/HTML/CSS 与 docs，未改后端 Python、未改数据库、未删除任何文件。
- 公网入口本轮未复测（cloudflared 隧道历史上间歇超时，按 AGENTS 不处置隧道基础设施）；验收以 owner 网关 `http://127.0.0.1:8000`（隧道所代理的同一上游）为准。

## 4. 测试摘要

- 隔离数据库测试（2026-09-23 重跑，隔离 schema `literature_ai_test`）：`tests/test_workbench_auth.py + tests/test_rebuild_workflow.py + tests/test_original_upload_paths.py` **29/29 通过**。
- 前端静态契约（运行目录实跑）：`tests/frontend_new_flow_static.spec.js` **6/6**；本轮新增 `tests/closeout_static.spec.js` **3/3**。
- 既有静态 spec 定向回归：同一批 12 个 spec 改动前 27 passed / 13 unexpected，改动后 28 passed / 12 unexpected，**新增失败 0 条**；剩余失败是既有过时断言（旧版工作台/文献库结构），与本轮无关。
- 真实浏览器验收（Chromium，owner 网关 `http://127.0.0.1:8000`）：本轮收尾 **55/55 通过、0 控制台错误**（窄屏六页面 24 + 数据表 14 + 汇总分析 17）；上一轮新流程适配 32/32（桌面 1440x1000 + 窄屏 390x844）。
- 覆盖上传幂等、无自动解析、去重/冲突、单位/常量、裁图/CSV、人工修正保留历史、条件分组不混合、数据表列收窄、窄屏无横向滚动。
- API 持久化回读：A0019 37 个图表对象、17 行数据、24 个值、24 个来源。
- source==runtime：`diff -rq` 核对 `frontend/pages`、`frontend/shared`、`frontend/tests`、`docs` 目录，**SOURCE==RUNTIME OK**。

## 5. 备份与回滚

### 数据资产校验

- PDF 数量：313 个
- PDF 总字节数：1,262,879,489
- PDF 清单 SHA-256：`941d2a707e70cdc3d678e77f04ec2893bb7eb679b229b20ffcf9d0f4635d601f`
- 本轮未删除、未改写任何已有 PDF/SI 或备份数据

### 发布前备份

- 目录：`/home/2401liyuhao/backups/literature-ai/value-edit-20260921T231717Z`
- 代码 SHA-256：`090b05a18d998651337a82126a6cb2d95ec0ad40c2aabf2c77d3172e74291a32`
- 数据库 SHA-256：`7f2f2de3f6e756f4a2ec49b4aece36592686a500b9e371c6e14793d7c3b96ae0`

### 发布后备份

- 目录：`/home/2401liyuhao/backups/literature-ai/postdeploy-20260921T232520Z`
- 代码 SHA-256：`7ab49d890f6fb59f1691aa9ba09c3ed5f2b2a3bd7fa138b5e72dde0b0ac9062c`
- 数据库 SHA-256：`9d15c5731c53096f06aefdca836d43aa3054663654d25a6b24318adae818332a`
- `pg_restore -l` 目录项：491

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

### 回滚方式

1. 停止 backend 容器。
2. 从备份目录恢复 `code.tar.gz` 中对应文件。
3. 从 `literature_ai-db.dump` 恢复 PostgreSQL。
4. 重启 backend 并复测健康与四页面。

## 6. 结论

- 核心目标流程已实现并上线；原版成熟页面与文件查看体验保留。
- A0019 已完成全图解释、真实取数、来源回看、汇总回归与 CSV 导出闭环。
- 2026-09-23 收尾项（数据表列收窄、汇总分析复测、窄屏复测、执行日志与验收报告同步）已通过执行方自检。
- 数据资产与登录保持不变。
- 当前仍保留旧审核数据作为历史兼容层，未删除、未批量改写。

### 已知边界（不得据此宣称独立验收通过）

- 以上均为执行方自检；总指挥/独立第三方的独立复验尚未完成。
- 公网入口本轮未复测；公网历史窄屏验证未完成，非代码缺陷（隧道问题）。
- 本轮未改后端 Python，隔离测试 29/29 属重跑确认无回归，不代表新增后端覆盖。
- 条件分组“不混合”证据为请求级机检（`condition_keys` + 样本签名一致性），未对全库所有反应逐一人工审阅。
