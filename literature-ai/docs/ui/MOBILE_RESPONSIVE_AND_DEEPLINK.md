# 工作台移动端/平板适配 + 跨页深链修复（2026-09-21）

## 背景

手机/平板上工作台多页无法使用（横向溢出、表格挤成一条、整页高度上万像素、
按钮占掉大半屏），并且多页跳转目标错误（例如审核中心点某篇文章，落到整个文献库列表）。
本次按"一页一页重做 + 修跳转"处理。

## 改动范围

改动分两部分：

1. `frontend/`（静态资源，卷挂载到 backend 容器 `/frontend`，改完即时生效，无需重启容器）——绝大部分工作。
2. `backend/` 4 个文件 7 处 URL 字符串（`aggregation.py`、`dft_review_queue_service.py`、
   `dft_export_service.py`、`paper_workbench_service.py`），把深链从"文献库列表"改成"论文详情"。
   卷挂载 `./backend:/app`，改完 `docker compose restart backend worker` 生效；
   已实测重启后 backend healthy、API 返回值正确、相关测试无新增失败。

未改数据库、未改 nginx 模板、未改 `.env`、未改 `docker-compose.yml`。

第二批续修：`shared/topnav.js`、`shared/topnav.css`（手机顶栏滚动 + 两端渐隐）、
`pages/dft_database/index.html`、`pages/dashboard/index.html`、
`pages/paper_detail/index.html`（性质深链）、`pages/review_center/page.js`（空态错位）、
`pages/literature_screening/page.js` + `page.css`（标题可点入口）、
`pages/extraction_workflow/index.html`（折叠段落半行裁切）。

第三批续修：`pages/ingestion/index.html`（断点 900 → 1080）、
`pages/mechanism_knowledge/index.html`（新增 1024–1280 档）、
`pages/literature_library/page.css`（981–1140 侧栏改浮层）。

第四批续修：`pages/dft_database/index.html`（筛选栏 561–1439px 重叠，见下文专节）、
`shared/topnav.js`（`extraction-workflow` 顶栏高亮 alias 由 `settings` 改为 `literature`）。
第五批续修：`shared/responsive.css`（`.litai-hscroll` 通用渐隐）、`shared/topnav.js`
（`TopNav.syncScrollHints()`）、`pages/extraction_workflow/index.html`、`pages/settings/index.html`
（横向条带可滑提示 + 分栏滚入可见区）。
第六批续修：`shared/topnav.css`（手机顶栏三按钮 32 → 38px、更多菜单条目 31 → 39px）。

到此为止，运行目录 `frontend/` 与 git 源 `/opt/ai-shujvku-src/literature-ai/frontend`
的差异合计 **29 个改动文件 + 1 个新增文件**（`shared/responsive.css`）；
`backend/` 另有 4 个文件 7 处 URL 字符串改动。

新增：`frontend/shared/responsive.css`（全站响应式基线，15 个页面引入）。
修改：`shared/topnav.js`、`shared/topnav.css`、`shared/components.css`，以及
`dashboard / review_center / literature_library / literature_screening / dft_database /
mechanism_knowledge / paper_detail / visuals / settings / ingestion / extraction_workflow /
ai_writer / content_knowledge / dft_audit_center / external_analysis_workbench / readonly`
各页的 `index.html` / `page.css` / `page.js`。

## 移动端结果（390×844，整页高度 px）

| 页面 | 首轮改前 | 现在（终值） | 说明 |
|---|---|---|---|
| review_center | 6174 | 5349 | 空态标题/说明改纵向两行（+23px，不再互相穿插） |
| dashboard | 1689 | 1557 | 「最近文献」标题整行 + 一行元信息，不再截成 "Unveiling the…" |
| mechanism_knowledge | 37271 | 11377 | hero 里「刷新聚合数据」从独占整行改为标题右侧小按钮 |
| literature_screening | 36838（99 篇不显示） | 18139 | 99 篇正常；标题现为可点入口 |
| settings | 1905 | 1352 | |
| visuals | 1275 | 1216 | |
| ingestion | 853（滚不动） | 2107 | 可滚 |
| literature_library | 844（滚不动） | 6123 | 可滚 |
| dft_database | 844（滚不动） | 6507 | 可滚；本轮再压 9193 → 6507（-29%） |
| extraction_workflow | — | 2588 | 折叠段落半行裁切修复（+2px） |
| readonly | 392 溢出 | 1046 | 无溢出 |

全站 6 档扫描（390 / 768 / 820 / 1024 / 1280 / 1440，17 页 × 6 = 102 条记录）：
横向溢出 0、console 报错 0、可见超宽元素 0
（`/tmp/litai-uicheck/FINAL14.json`，终版代码实测；FINAL9→FINAL10→FINAL11→FINAL13→FINAL14
五轮逐条高度完全一致，0 条变化）。
另有 5 档（390 / 660 / 900 / 1000 / 1024 / 1081）× 17 页 = 85 条"半行裁切"检测，命中 0
（`/tmp/litai-uicheck/LINECUT-final.log`）。

桌面 1440 逐页高度与首轮基线一致（`dashboard 1029`、`dft_database 900`、
`mechanism_knowledge 9979`、`extraction_workflow 2441`、`review_center 4134`、
`literature_library 2141`、`literature_screening 11657`、`settings 1507`、`visuals 1040`、
`ingestion 909`、`ai_writer 1492`、`paper_detail 900`）。

## 跨页跳转修复

1. **审核中心 / DFT 数据库 / 机理知识聚合 / 文献库** 的文章链接统一指向
   `../paper_detail/index.html?paper_id=<uuid>`，不再落到文献库列表页。
   `mechanism_knowledge` 原有 266 处、`dft_database` 25 处错误形态已全部纠正（实测 0 残留）。
2. **`paper_detail` 结构化深链**：支持 `paper_id` + `tab` + `target_type` + `target_id` +
   `issue_id` + `field_name` + `pdf_page` + `pdf_locator_status` + `pdf_evidence_text` +
   `library_name`，进入后顶部显示定位条（到了哪篇/哪个区域/哪个对象/PDF 第几页/目标 ID/证据原文），
   自动切 tab、高亮目标卡片并滚动到位。
3. **tab 名兼容**：旧链接用的 `writing` / `mechanism` / `content` / `review` 等 tab 名，
   在 `paper_detail` 里通过 `DETAIL_TAB_ALIASES` 映射到真实 tab（`cards` / `sections` / `summary` …）。
   改动前这些值会让 `switchDetailTab` 抛 `TypeError`，导致详情页直接显示"加载失败"。
4. **手机端深链动作条**：≤700px 时顶部定位条压成纯信息行（隐藏证据原文），
   「打开 PDF 第 N 页 / 返回审核中心」移到底部固定条（49px），不用滚回顶部才能点。
5. **顶栏"更多"菜单**：只保留真实存在的 5 个工具页
   （论文入库 / 文献筛选 / 机理知识聚合 / 本地 AI 写作 / 高级提取协议）。
   `dft_audit_center`、`content_knowledge`、`external_analysis_workbench` 现在只是历史 URL
   （打开会 `location.replace` 跳回审核中心），不再列进菜单，避免"点了没去对地方"。
6. **反向跳转（详情页"返回"）也是来源感知的**：从哪个页面进来就回哪个页面，
   不再固定回文献库 / 审核中心，详见下文「第七批续修」。

## 本轮续修细节（同日第二批）

1. **手机顶栏（全站生效）**：≤768px 主导航本就是横向可滑动，但没有可见性提示、
   且当前页签常停在可视区外。现在 `topnav.js` 会上来把当前页签滚进可见区，
   并按滚动位置给两端加渐隐（`is-scrollable` / `not-start` / `at-end`）。
   实测 390px：`.topnav-items` clientWidth 227 / scrollWidth 360，进 `设置` 页时
   `设置` 已在可视区内；此前它是被裁掉、且看不出还能滑动。
2. **`paper_detail` 支持 `property_type` 深链**：审核中心与 DFT 数据库的行链接都带
   `tab=dft&property_type=adsorption_energy` 这类参数，但详情页只认 `target_type/target_id`，
   于是"切到 DFT 标签页"却不定到具体性质分组。现在会按性质定位 `.dft-prop-section`、
   逐级展开所在催化剂 `<details>`、高亮 + 滚动到位，顶部定位条显示「性质：…」。
   实测 7 种性质（adsorption_energy / zero_point_energy_correction / entropy_correction_ts /
   bader_charge_transfer / bond_length / reaction_barrier / adsorption_energy_solvated）
   全部 found + highlighted + 滚入视口。
3. **文献筛选页的标题成为入口**：此前 99 条候选只能勾选，无法打开论文。
   现在标题链到 `../paper_detail/index.html?paper_id=<uuid>`（390 / 1440 实测点击落到同一篇）。
4. **折叠段落"半截第三行"彻底修掉**：`overflow` 的裁剪边界是 padding box，
   `.advanced-note` 有 10px 下内边距时，2 行高度的上限仍会露出第三行 6px。
   现把 2 行高度交给无内边距的 `.advanced-note-body`（并去掉底部渐隐遮罩）。
   专用检测脚本 `linecut-audit.js`：改前 1 处 → 改后全站 0 处。
5. **审核中心空态错位**：`page.js` 里 `empty.style.display = "flex"` 的 inline 样式
   盖掉了页面自己的 `@media (max-width: 700px) { display: grid }`，
   手机上「先选择一篇主文献」与换行后的说明被排在同一行、看起来互相穿插。
   改为 `empty.style.display = ""` 交回 CSS 决定。
6. **审计脚本自身的假阳性**：`audit.js` 原先把"元素右边界超出视口"一律算问题，
   但单行省略号标题里被 `overflow:hidden` 裁掉的下标（`<sub>`）并不可见。
   现已按祖先裁剪矩形判断可见部分（dft_database 390px 原有 25 个 `SUB` 假阳性，现为 0）。
7. **论文入库页的断点从 900px 提到 1080px**：901–1080px（平板横屏 / 小笔记本）此前仍是
   `100vh` 固定高布局，虚线框被 flex 居中顶上去**压住「上传 PDF」胶囊**，
   日历也被裁掉一整行。现该区间改走与手机同一套自然纵向滚动布局（>1080px 完全不受影响）。
   实测「上传 PDF」按钮底边与虚线框顶边：原始版 820/900/901/1024/1080 全部**重叠 +21px、
   日历被裁 106px**；现在 820–1080 为 **-15px 间隙、日历裁切 0**，1081+ 仍是 -23px / 0。
8. **机理知识聚合页新增 1024–1280px 一档**：此前只有"手机"和"桌面"两套，1024–1280 沿用桌面
   的字段/关系卡片，一条记录占掉整行、页面高度失控。现该区间只把字段/关系项压成流式小标签
   （hero 不动）。实测整页高：1024 **19039 → 9460**、1100 **13377 → 8524**、
   1180 **13377 → 7607**、1280 **13330 → 6757**；≥1281px 桌面不变（1281 = 13330、1440 = 9979）。
9. **文献库 981–1140px 由"侧栏 + 列表并排"改为筛选浮层**：这一档列表区只剩 ~635–794px，
   标题列被压到 0–57px、行高被撑到 281px（一条记录占大半屏）。现把筛选栏收成浮层
   （与 ≤980px 同一套交互：`#filterOpenBtn` 开、`#filterCloseBtn` 关），列表区回到 ~970px；
   同时把"收起类型 / IF"的区间从 `701–900 + 981–1140` **收窄为只 `701–900`**，
   于是在 981–1140 这两列重新显示。实测（同一篇数据）：1140 标题列 **57 → 512px**、行高 **281 → 79px**；
   1100 标题列 **17 → 472px**、行高 **281 → 79px**；1024 标题列 **0 → 396px**、行高 **281 → 86px**；
   1440 桌面不变（标题列 357px / 行高 86px）。

## 第四批续修：DFT 数据库筛选栏在 561–1439px 互相重叠（本轮新发现）

这是前几轮审计都漏掉的一类缺陷：不是"溢出视口"，而是**控件压住彼此**，
所以 `ovf=false` / `bad=0` 的扫描结论掩盖了它（旧的 `visibleRight()` 把
`overflow:auto` 的祖先一律判成"可滚动，不算问题"）。新写了
`/tmp/litai-uicheck/overlap-audit.js`（可见交互元素两两求交，重叠面积超过较小者 25% 即记一条）才暴露。

- **根因**：`.filter-field { min-width: 0 }` + `.dft-toolbar/.dft-filter-grid { flex-wrap: nowrap }`。
  整行不换行时 flex 会把每个 `.filter-field` 压到比自己的内容还窄，而里面的
  `select`（150px）/`#minConfidence`（88px，但全局 `input{min-width:92px}`）不会跟着缩，
  于是溢出盒子、直接盖到右边那个筛选项上。
- **实测重叠对**（改前，与改前基线快照逐条一致，属历史遗留、非本轮引入）：
  1024×768 **11 处**（控件右侧溢出 31–82px）、1081×900 **9 处**、1280×900 **2 处**（37px）、
  1366×900 **2 处**（26px）、1420×900 **1 处**（18px）、1440×900 **0**。
  截图可见「全部文献库」压住「能量类型」、「Li2S4, Li2S8, H2O」压住「催化剂」、
  「0.30」压住「导出数据范围」、「通用全部（不限体系）」压住「导出 CSV」。
- **修法**：新增 `@media (min-width:561px) and (max-width:1439px)`——
  `.dft-toolbar` / `.dft-filter-grid` / `.dft-export-panel` 允许换行、
  `.dft-filter-grid .filter-field { min-width: max-content }`（永不被压到比内容窄）、
  `overflow-x: visible`（避免 `overflow-x:auto` 把纵向也算成 auto 而裁掉换行后的第二行）。
  下界取 561px 是为了不和 `≤560px` 的两列网格规则打架；上界取 1439px 是为了让
  **≥1440px 桌面完全不变**（1440 实测工具栏仍是 32px 单行，与改前逐像素一致）。
- **改后实测**：1024/1081/1280/1366/1420/1440/1600/1920 **全部 0 重叠**。
  代价是 561–1439px 会多一行：1024×768 工具栏 40→116px、表格窗 545→461px；
  1280×900 工具栏 40→116px、表格窗 677→593px。**≥1440px 工具栏仍 32px，表格窗 677/857px 不变**。
- **全站复查**：`overlap-audit.js` 跑 17 页 × 390/820/1024/1280 共 **68 条记录，全部 0 重叠**；
  全站扫描 `FINAL10`（6 档 × 17 页 = 102 条）仍是 `ovf=0 / errs=0 / bad=0`，
  且与 `FINAL9` 逐条高度**完全相同**（0 条变化）。

## 第五批续修：藏起来的横向滚动条带没有"可滑"提示

同一类"手机上看不出右边还有内容"的缺陷，这次是**滚动条被隐藏的横向条带**：

- `extraction_workflow`：7 步流程步骤条（`#stepList`，390px 下 1053px 内容塞在 348px 窗口里、
  藏 705px）和 hero 徽章条（藏 241px）。切口正好落在第 3 步中间（"3 R…/拆"），
  看起来像布局坏了，而这一步其实是页面主要交互入口。
- `settings`：分栏导航 `.section-nav`（藏 26px，"使用说明"被切掉一角），
  而且点它 / 带 `#guide` 打开时分栏虽然高亮，却停在右侧被裁的位置。

- **修法（一次实现，全站可用）**：把"两端渐隐"抽成共享能力——
  `shared/responsive.css` 新增通用类 `.litai-hscroll`（`is-scrollable` / `not-start` / `at-end`
  四态 mask，与顶栏 `.topnav-items` 同一套语义），
  `shared/topnav.js` 新增 `TopNav.syncScrollHints()`（扫 `.litai-hscroll`、按溢出情况加/去状态类、
  绑 scroll/resize/orientationchange），并在 `TopNav.init()` 里自动跑一次。
  三处滚动容器加 `class="... litai-hscroll"` 即可；不溢出时不会加类，所以桌面完全不受影响。
- `settings` 另加 `scrollSectionNavIntoView()`：切栏/`#guide` 深链后把当前分栏滚进可见区
  （与顶栏 `_syncActiveItem()` 同一做法）。
- **实测**（`.timeline-list` / `.hero-badges` / `.section-nav`）：
  320px 全部 `is-scrollable` + 右渐隐（藏 775 / 311 / 96px）；
  390px 同样（藏 705 / 241 / 26px）；700px 只有步骤条溢出（藏 395px）；
  768px 与 1440px 三者都 `藏 0px`、`mask=none`（**桌面/平板零影响**）；
  点 settings 最后一栏后 `scrollLeft=26、fullyVisible=true`；console/JS 报错 0。

## 第六批续修：手机顶栏按钮小于触摸目标

- **问题**：≤620px 时顶栏右侧「更多 / 退出登录 / 主题」三个按钮是 **32×32px**，
  低于 `shared/responsive.css` 自己声明的 `--touch-target: 40px`；
  「更多」菜单里的 5 个入口只有 **31px** 高。手机上这三个是最高频的操作（主导航本身要横滑）。
- **修法**：`shared/topnav.css`——`.topnav-more-btn` / `.topnav-theme-btn` / `.topnav-session-btn`
  在 ≤900px 一律 **38×38px**（导航栏由 `.topnav-item` 撑到 40/41px，高度不变），
  `.topnav-more-item` 内边距 7px → 11px（**31px → 39px**）。
- **实测**（320 / 360 / 390 / 414 / 620 / 768px）：三按钮都是 **38×38**、
  栏高仍 41px、横向溢出 false、JS 报错 0；
  390px 下把页签条压到 209px 仍能横滑且当前页签自动滚入可视区（320px 为 139px）。
- **手机「更多」菜单可点性实测**（390px）：面板完整落在视口内、5 个条目都完整可见且高 39px、
  逐个请求都是 200、再点一次能收起。

## 第七批续修：跨页"返回"改为来源感知（每个入口回自己那一页）

- **问题（实测确认）**：`paper_detail` 的返回是写死的——面包屑与「返回列表」按钮固定
  `../literature_library/index.html`，定位条与手机底部动作条上的按钮固定「返回审核中心」。
  从 DFT 数据库 / 文献筛选 / 机理知识 / 工作台 / 图表点进详情后按"返回"，
  会被丢到**文献库列表页**（实测 8 个入口 100% 复现）；反过来从这些页面进来的用户
  也看不到与来源相关的返回入口。
- **修法**：`paper_detail` 新增 `RETURN_SOURCES` 表 + `returnSourceKey()` / `returnHref()`
  / `applyReturnSource()`，按三级优先级判定来源：
  ① URL 的 `from` 参数 → ② `document.referrer` 同源路径里的 `/pages/<page>/` →
  ③ 上下文兜底（带 `issue_id` 的深链仍回审核中心，其余回文献库）。
  判定结果统一驱动面包屑首项、右上「返回<来源>」按钮、空态页返回按钮、顶部定位条与手机底部动作条。
- **入口页同步带 `from=`**（不依赖 referrer，`target="_blank" rel="noopener noreferrer"` 会剥掉 referrer）：
  `review_center/page.js`（6 处）、`dashboard/index.html`（2 处）、`dft_database/index.html`、
  `literature_library/page.js`（行点击 + 键盘 Enter）、`literature_library/detail-loader.js`、
  `literature_screening/page.js`、`mechanism_knowledge/index.html`、`visuals/page.js`、
  `dft_audit_center/index.html`。
- **`literature_library` 的返回链接故意不带 `paper_id`**：该页带 `paper_id` 会
  `location.replace` 回详情页，带上会形成"返回 → 又被送回详情"的死循环。
- **顺带修好一类深链**：`deepLinkHasContext()` 原来只认 `target_type/target_id/issue_id/pdf_page/
  field_name/property_type`，不认 `tab`，所以审核中心「查看全部 DFT 数据」这种只有
  `paper_id&tab=dft` 的链接进详情页后**没有任何定位条与底部动作条**（手机上没有"打开 PDF / 返回"入口）；
  现在 `tab` 也计入上下文。
- **实测（真实点击，BASE=http://172.18.0.6:8000，1440×900，pageerror=0）**：

| 入口 | 落地 URL 的 `from` | 面包屑 | 返回按钮目标 |
|---|---|---|---|
| 审核中心 | `review_center` | 审核中心 | `../review_center/index.html?paper_id=…` |
| DFT 数据库（`target=_blank`，referrer 为空） | `dft_database` | DFT 数据库 | `../dft_database/index.html?library_name=…` |
| 文献筛选 | `literature_screening` | 文献筛选 | `../literature_screening/index.html` |
| 机理知识聚合 | `mechanism_knowledge` | 机理知识 | `../mechanism_knowledge/index.html` |
| 工作台 | `dashboard` | 工作台 | `../dashboard/index.html` |
| 文献库（行点击） | `literature_library` | 文献库 | `../literature_library/index.html`（不带 paper_id） |
| 图表（相关性矩阵下钻） | `visuals` | 图表 | `../visuals/index.html` |
| DFT 审核中心 | 该页是存根，`location.replace` 到审核中心 | 审核中心 | `../review_center/index.html?…` |

- **没有来源信息时**（无 `from`、无 referrer、无 `issue_id`）：仍回文献库，
  即老书签 / 直接粘贴 URL 的行为与改动前一致，不会 404 或跳空。
- **顶栏高亮不动**：详情页仍高亮「文献库」（`nav-active.js` 15/15 PASS），
  来源提示交给面包屑，避免同一页面因入口不同而让顶栏高亮乱跳。

## 平板（768 / 820 / 1024）专项

- **文献库表格在 701–1280px 区间不再裁列**。基础规则给「来源 / DOI」235px、「状态」230px，
  窄屏下把标题列挤到 0 宽、右侧状态列被切掉。现按窄屏口径重新分配列宽
  （701–900px 与 981–1140px 各用一套：前者列表区约 790–950px，后者约 966–1082px），
  只在 **≤900px** 这一段收起「类型」「IF」两列；981–1140px 侧栏已改浮层、列表够宽，两列恢复显示。
  实测 701/768/820/900/901/980/981/1024/1140/1141/1280/1281/1440 全部无横向滚动、
  「状态」列完整可见；标题列终值：820 → 334px、980 → 380px、1024 → 396px、
  1100 → 472px、1140 → 512px、1440 → 357px。
- **库里标题的 `<sub>` 不再被印成字面标签**。数据里标题含 `FeN<sub>4</sub>` 之类，
  该页原来整串 `esc()`，手机上直接显示 `FeN<sub>4</sub>`。现只放行 `sub/sup`、其余照旧转义
  （与 `dft_database` 同一处理）。1440 桌面整页高度 2180 → 2141px，减少的是标题少折行，非布局变化。
- 审核中心表头（`.review-table-head` 980px 宽）在 820 会被审计脚本判为"超宽元素"，
  属**假阳性**：它在 `overflow:hidden` 的 shell 里，且用 `translateX(-scrollLeft)` 跟随表格横向滚动
  （`review_center/page.js:774`），页面级横向溢出为 0。

## 兼容性说明

- **后端已直接产出正确链接（本轮修根因）**：`library_detail_url` / `detail_url` 原来是
  `../literature_library/index.html?paper_id=…&tab=dft|review`，也就是"点进去落到文献库列表"的
  真正来源。现在 4 个文件共 7 处改成
  `../paper_detail/index.html?paper_id=…&tab=dft|review`：
  `backend/app/api/papers/aggregation.py:286`、
  `backend/app/services/dft_review_queue_service.py:108/378/387`、
  `backend/app/services/dft_export_service.py:821/825`、
  `backend/app/services/paper_workbench_service.py:815`。
  实测 `/api/papers/export/dft-quality` 72 条、`/api/papers/export/dft-review-queue` 均已是
  `../paper_detail/index.html?paper_id=…&tab=dft`，用这些 URL 打开页面 pid 一致、`tab=dft`、
  jsErr=0。前端仍保留旧书签兜底（见下），所以旧链接不会 404。
- **前端兜底保留**：`literature_library` 带 `paper_id` 访问时 `location.replace` 到
  `paper_detail` 并保留全部 query（旧书签、外部调用不会被截断）。
- `review_workbench_url` 仍是指向 `../external_analysis_workbench/index.html?paper_id=…`，
  该页是"立即 replace 到 review_center 并保留 query"的存根，实测 paper_id 保留、落到复查中心。
- `tests/smoke.spec.js` 中 3 组断言的是"文献库自带 figures/mechanism/writing/DFT 标签页"的旧 UI
  （`literature library reuses content data for the mechanism tab` 等），
  这些断言在本次改动**之前**就已经失败，属于历史遗留，不是本次引入。

## 验证方式

> 回归必须用**安全源**跑（`http://127.0.0.1:4173`，即 `playwright.config.static.js` 自带的
> `npm run test:serve`）。用 `http://172.18.0.6:8000` 这类非安全源时，
> `navigator.clipboard` 不可用，会让审核中心 4 个"复制命令"用例假失败。

- 移动/平板扫描：`/tmp/litai-uicheck/audit.js`（17 页 × 390/768/820/1024/1280/1440，
  检查横向溢出、console 报错、可见超宽元素）。终版结果（第七批之后重跑，`FINAL15.json`，
  与 `FINAL14.json` **逐条零差异**）：102/102 条 `ovf=false`、`errs=0`、`bad=0`、`ok=true`。
- 半行裁切检测：`/tmp/litai-uicheck/linecut-audit.js`（按 Range 行框 × 裁剪边界判断"是否只露出半行"）。
  终版结果：85/85 条 `linecuts=0`，收尾再扫 15 页 × 390/1024 共 30 条仍全为 0。
- 慢页面复测：`/tmp/litai-uicheck/heavy-ovf.js`（14s 静置，确保数据渲染完成）。
  `paper_detail / review_center / literature_screening / mechanism_knowledge / dft_database`
  在 390/768/820/1024 四档共 20 条记录，横向溢出 0、超宽可见元素 0、console/JS 报错 0
  （快速扫描对这几个重页面会读到"尚未渲染完"的高度，所以另跑一遍长静置）。
- 控件互相压住检测：`/tmp/litai-uicheck/overlap-audit.js`（可见交互元素两两求交，
  重叠面积 > 较小者 25% 即记一条）。终版：17 页 × 390/820/1024/1280 = 68 条，全部 0 重叠。
- 超大操作块检测：`/tmp/litai-uicheck/bigmodule.js`（390px 下按钮高 > 18% 视口、
  或"低内容高模块" > 240px）。终版：17 页全部 0（对已折叠 `<details>` 里的内容用
  `checkVisibility()` 过滤，否则会误报 review_center 的 `#dftDirectMethodSection`）。
- 手机顶栏与字号核对：`/tmp/litai-uicheck/mobile-nav-and-font.js`
  （390px 顶栏「更多」菜单开/关、5 个入口可达性、三按钮尺寸；以及 10 个页面
  390px vs 1440px 的 body/h1 字号对比，用于证明"字体确实缩小"，例如
  `mechanism_knowledge` h1 42 → 18px、`review_center` 31.7 → 17px、`dashboard` 26 → 19px）。
- 横向条带渐隐核对：`/tmp/litai-uicheck/hint-probe.js`、`hint-probe2.js`
  （320/390/700/768/1440 五档 × `.timeline-list`、`.hero-badges`、`.section-nav`）。
- 横向"藏内容"扫描：`/tmp/litai-uicheck/hscroll.js`（390px 下找 `overflow-x` 可滚且
  `scrollWidth - clientWidth > 24` 的可见容器）。终版除顶栏与上述三处（均已带渐隐提示）外，只剩
  `readonly`/`share` 为 0。
- 顶栏高亮核对：`/tmp/litai-uicheck/nav-active.js`（14+1 个页面，检查顶栏 `.topnav-item.active`
  是否与该页所属板块一致）。终版 **15/15 PASS**；修 `extraction_workflow` 之前是 13/14
  （它把 `NAV_ALIASES` 里的 `extraction-workflow` 映到了 `settings`，于是「更多 → 高级提取协议」
  打开后顶栏高亮在「设置」，而同样在"更多"里的另外 4 个工具页都高亮「文献库」）。
- 全站链接核对：`/tmp/litai-uicheck/href-audit.js`（每页所有 `<a href>` 解析成绝对地址逐个请求）；
  终版结果：15 页、**158 个唯一目标、异常 0**、各页 `jsErr=0`
  （唯一目标从 126 涨到 158，是因为"来源感知返回"给同一目标加了不同的 `from=` 变体）。
- 返回目标核对：`/tmp/litai-uicheck/return-audit2.js`（8 个入口**真实点击**进详情，
  读面包屑/返回按钮/定位条/底部动作条）、`return-audit3.js`（文献库行点击）、
  `return-audit4.js`（DFT 数据库表格链接，`target=_blank` 且 `rel=noopener noreferrer`）、
  `return-audit5.js`（图表→相关性矩阵下钻）、`return-phone.js`
  （390/768 双档 × 7 种来源，检查溢出与按钮可点性）。
  终版结果：8/8 入口落到正确来源页、`pageerror=0`；
  390px 下 `ovf=false`、底部动作条两个按钮各 183–185px 宽且完整在视口内、按钮高 36px。
- 深链落地核对：`/tmp/litai-uicheck/deeplink-e2e.js`（审核中心 11 条深链全部带 `paper_id`、定位条可见、pageerror 0）、
  `proptype-e2e.js` 与 `prop-diag.js`（7 种性质全部 found + highlighted + 滚入视口，含 390/1440 双档复测）、
  `nav-final.js`（跨页跳转 **13/13 PASS**）、`ls-link.js`（文献筛选 390/1440 各 99 条标题链接，pid 一致）、
  `nav-strip.js`（手机顶栏）。
- 改前/改后对照：`/tmp/litai-uicheck/before-proxy.js` 把改前快照当静态根 + `/api` 反代真实后端
  （基线快照 `/tmp/litai-base-385499/literature-ai/frontend`，监听 `127.0.0.1:4180`）。
- 回归：18 个 static spec 组成的聚焦集（71 个用例）在改前基线与改后运行结果**逐条一致**
  （改动过程中在该聚焦集上跑了 7 次：focused-after4/5/6/7/8/9/10，每次都是 0 差异），
  0 个新增失败、0 个新增通过、0 个单侧缺失（两侧均为 42 passed / 27 failed / 2 timedOut，
  失败均为既有环境性失败）。

```bash
cd /opt/AI-shujvku/literature-ai/frontend
TEST_BASE_URL=http://172.18.0.6:8000 node node_modules/@playwright/test/cli.js \
  test tests/paper_detail_ui_static.spec.js tests/review_center_title_metadata_static.spec.js \
  --config=playwright.config.static.js --reporter=line
```

后端改动的验证（在 backend 容器内跑，不碰生产库，用隔离的 `literature_ai_test`）：

```bash
cd /opt/literature-ai && docker exec literature-ai-backend-1 sh -c 'export LITAI_TEST_ROOT_DATABASE_URL=$(echo "$LITAI_DATABASE_URL" | sed "s#/literature_ai\$#/literature_ai_test#"); python -m pytest tests/test_export_safety_gate.py -q -p no:cacheprovider'
```

实测：`test_export_safety_gate.py` 48 passed / 1 failed，
`test_b0102_dft_contract_fixes.py + test_dft_review_display_status.py + test_dft_ml_dataset_v3.py + test_dft_ml_dataset_v3_api.py + test_dft_ml_first_policy.py + test_codex_workbench_v1.py`
合计 136 passed / 3 failed；**这 4 个 failed 在改前版本上逐条复现，均为既有失败，非本次引入**
（已用改前备份文件回滚复测确认）。

## 发布注意事项

`frontend/` 下本次改动的文件**都在 git 源 `/opt/ai-shujvku-src` 里**，而 `update.sh` 用
`rsync --delete` 同步。如果不把这些改动提交进 GitHub，下次有人在服务器跑 `update.sh`
会把本次改动整体回滚（新增的 `shared/responsive.css` 会被删除）。改动清单见本文"改动范围"。

## 快跑工具（`frontend/tools/`，2026-09-21 落地）

本次的审计脚本原来散在 `/tmp/litai-uicheck/`（每次要重写、且串行跑一次全站要 25 分钟）。
现在收敛成仓库内的四个工具 + 一个入口脚本，并已用"与历史基线逐条对比"验证过等价性：

| 工具 | 作用 | 实测 |
|---|---|---|
| `tools/audit-responsive.js` | 响应式扫描，**并行**（每个"页面×档位"仍是独立 context，保持与旧脚本同样的量法） | 102 条 **193.5s**（串行约 1500s，**7.8×**），结果与 `FINAL15.json` **逐条 0 差异** |
| `tools/audit-diff.js` | 两份扫描结果逐条对比，只打差异（`sh`/`ovf`/`errors`/`bad` 变化、新增缺失） | 替代原来的一次性 python 对比 |
| `tools/link-audit.js` | 全站内部链接体检：唯一目标逐个请求、`paper_detail` 链接必须带 `paper_id`、统计每页"进入详情"入口（含 `tr[data-paper-id]` 行点击） | 15 页、**160 个唯一目标、异常 0** |
| `tools/return-nav-check.js` | 8 个入口**真实点击**进详情，断言面包屑/返回按钮/定位条/底部动作条都回来源页 | **PASS=8 FAIL=0 SKIP=0** |
| `tools/ui-check.sh` | 入口：`verify` / `full` / `spot` / `links` / `jumps` / `baseline` / `shots` / `diff` | `spot`（6 页 × 2 档）**37s**；`verify` 三件套 **6m15s** |
| `tools/baselines/` | 冻结基线 + `manifest.json` + 每份基线的量法元数据（`SETTLE`/`STABLE`/并发/源） | `frozen-2026-09-21a`（= 历史 `FINAL15`）、`ui-2026-09-21`（重跑带 meta） |

```bash
# 日常"改完了验一下"：spot + links + jumps，实测 6m15s，三个都通过才返回 0
/opt/literature-ai/frontend/tools/ui-check.sh verify

# 迭代期：只抽查最容易回归的页面（默认 6 页 × 390/1024，约 40 秒）
/opt/literature-ai/frontend/tools/ui-check.sh spot

# 收尾：全站 17 页 × 6 档；不传参时自动与 tools/baselines/ 最新基线逐条对比（有差异退出码 1）
/opt/literature-ai/frontend/tools/ui-check.sh full

# 确认这批结果没问题后，冻结成新基线（以后 full 自动和它比）
/opt/literature-ai/frontend/tools/ui-check.sh baseline ui-2026-09-21

# 跳转专项
/opt/literature-ai/frontend/tools/ui-check.sh links
/opt/literature-ai/frontend/tools/ui-check.sh jumps
```

- **基线自带量法元数据**：每份基线旁边有 `*.meta.json`（`SETTLE`/`STABLE`/并发/`BASE`/页面档位），
  `full` 对比时若发现量法不同会打印 `⚠ 量法不一致，对比可能失真` —— 这类"0 差异"不可信。
  实测该提醒可用：拿 `SETTLE=2000` 的结果去比 `SETTLE=6000` 的基线，四项差异全部被点出。

- **抽查清单**由 `ui-check.sh` 顶部的 `SPOT_PAGES` 决定（当前：`paper_detail,literature_library,
  review_center,dft_database,literature_screening,mechanism_knowledge`），改动面变了就改这一行。
- **并行不会改变结论**：每个"页面 × 档位"依旧用全新 context（避免 localStorage 串页），
  只有调度并行；验证方式就是拿并行结果和串行冻结基线逐条 diff（0 差异）。
- **两个必须知道的旋钮**：`SETTLE`（默认 6000ms）会改变量到的高度，**和历史基线对比时必须与
  基线一致**；`STABLE`（默认 2500ms，设 0 关闭）是"连续 2500ms 高度不变才算渲染完"的安全网，
  本次实测开着与关掉结果相同（值都稳定），但它能防"机器繁忙时量到半张页面"的假结论。
- **慢页面**：`dft_database`（约 24s 才出表格）、`visuals`（要等热力图自动选中）、`review_center`
  需要更长静置，`link-audit.js` 里用 `PAGE_SETTLE` 表覆盖，否则会漏检它们的链接（早期版本就漏了）。
