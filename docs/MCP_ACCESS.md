# 网页 AI 接入新版文献工作台（MCP v2）

ChatGPT / Claude 通过远程 MCP 自己阅读与分析文献，再将结果保存到新版图表、数据和导读中。
服务器不替网页 AI 调用模型，不启动 Codex 对话，不自动选择或续排下一篇。

## 连接

- Server URL：`https://dft.researchlife.top/mcp`（Streamable HTTP）。
- 网页客户端使用 OAuth；客户端 ID 为 `chatgpt-literature-ai`，密钥和授权登录凭据沿用服务器私有配置，禁止贴入聊天或提交仓库。
- 保留已有 OAuth 回调白名单。新连接若产生新回调，必须精确核对后追加，不能开放任意回调。
- 内部客户端仍可使用既有 Bearer MCP key，但必须显式授予所需权限。
- `read_papers` 只读；`write_rebuild` 才能建立本篇工作与保存新版成果。旧 `propose_corrections` / `request_parse` 权限不会自动获得新写权限。
- 旧 OAuth token 不会凭空增加写权限；需在网页重新连接并授权新 scope。旧工具名不再兼容，客户端应刷新工具列表。

ChatGPT 的具体入口随客户端版本可能显示为插件、应用或自定义 MCP；在创建连接时选择 OAuth。
连接成功只代表协议可用；必须另验真实资料读取和工作台结果显示。

## 工具（20 项）

| 工具 | 用途 |
|---|---|
| list_libraries_and_papers | 按库、标题、DOI 或编号查主文献，稳定 paper_id 与有界分页 |
| get_paper_workspace | 新版已有成果、来源、修订、合同、缺项及自身外部工作状态 |
| list_paper_files | 当前正文/SI、首选文件、历史与文件集修订 |
| read_source_text | 带来源哈希的 PDF 页或 DOCX OOXML 段落/表格 |
| view_source_page | 返回真实 PDF 物理页的 MCP 图片内容 |
| view_figure_image | 返回本篇已有图表的 MCP 图片内容 |
| list_data_rows | 本篇新版数据行、精度、来源、缺失及冲突 |
| get_reading_context | 摘要与导读输入合同；全文另用分页读取工具 |
| get_quality_report | 有界质量问题；不代表科学 accepted |
| get_data_templates | 当前反应模板与字段 |
| begin_paper_work | 人工核对后建立单篇工作和占用，备份旧成果，不派发模型 |
| save_figure_assets | 保存图表、表格资料与解释 |
| crop_figure | 按真实 PDF 区域生成不可变裁图 |
| preview_data_rows | 在独立回滚事务中预检数据，无持久写入 |
| save_data_rows | 逐行逐格保存带来源的数据，返回部分失败回执 |
| save_sections | 可选的来源化章节解释 |
| save_reading_assistance | 保存摘要翻译与中文导读 |
| report_paper_work | 持久报告本次文件阶段、具体缺项和待核实情况 |
| finish_paper_work | 根据读回结束为 completed/partial/failed |
| get_write_receipt | 读取自身工作历史回执，不冒充当前结果 |

除无参数的 get_data_templates 外，各工具参数位于 `request` 对象中；以 tools/list 的 inputSchema 为准。

## 每篇使用方法

### 2026-10-28 候选：直接保存

持有 `write_rebuild` 的调用方在没有其他未结束同篇写者时可以省略 `work_id` 和 `expected_revision`，逐项将图表、数据、章节或阅读内容保存到同一篇文献。存在未结束写者时，只有同一 actor 和 `work_id` 的续写放行；精确保留的原请求回执先于当前占用检查重放。其他 actor、无 work 令牌的并发新写或同 request_id 改内容返回明确错误，不新增成果。`save_reading_assistance` 推荐只在外层 `request.work_id` 提供工作身份；旧客户端的嵌套 `reading.job_id` 仍兼容，但两者同时出现且不同返回 `work_identity_mismatch`。阅读保存必须匹配当前 input/reading/abstract source revision；`main_and_si` 缺正文返回 `main_source_required`，仅有正文而无 SI 时兼容降级为 `main_text` 或 `partial_text` 并在 missing 记录原因。`abstract_only` 无明确摘要来源时保留原兼容合同，降级为 `full` 任务。短数据库事务用于完整写入，重复内容通过原有身份合并。

1. 查找论文，核对稳定 `paper_id`；阅读 workspace、当前正文/SI、已有结果和当前占用。
2. 用户选定有限范围后，使用新的稳定 `request_id`、当前 `file_set_revision` 与明确 `file_ids` 调用 begin_paper_work；有未解决原生/外部写者或独审任务则拒绝。
3. 工具返回 `work_id`、`work_revision`。所有保存、回报和结束均携带自身 work_id 和最新修订作为 expected_revision；阅读保存优先使用外层 work_id。每个新操作使用新 request_id；丢失响应时只重放完全相同的原请求，原工作结束后仍可取得历史回执。
4. 逐文件读取原文，必要时看真实原页，保存来源化图表和数据。每个非空值必须有证据；保留原精度、单位、材料与条件，估读标识和依据必填。看不清、未报告留空并写原因。
5. 检查回执逐格 `errors/outcome/persisted` 并读回。HTTP/MCP 调用成功不等于全部保存；409 后重新读取核对，不盲换 ID 覆盖。
6. 中文导读使用当前 reading context 的 input_revision 和摘要来源修订；必须把所有当前来源纳入本次工作，避免用未授权来源生成整篇导读。保留原文和其他已有导读命名空间。
7. 读回后 finish。completed 需要每个文件的 read 完成，以及图表/数据成果；没有可提取内容的阶段用 not_applicable 并给出真实原因。部分结果可用 partial 结束，成功成果保留。结果不代表科学 accepted。

外部工作无自动超时释放：断线、unknown、needs_attention 及没有原生终态证据的 failed 保留占用，重连读取原 work_id 状态。来源替换、哈希变化、未知占用需受控核对，不能为继续而删除原任务或改借他人的工作 ID。

## 备份、身份与边界

begin 在允许新写者进入前，给本篇现有主记录、文件元数据、图表、数据、来源和章节创建不可变 JSON 备份，位于服务器项目 data 内的 `storage_root/mcp-v2-backups/<paper_id>/`。原 PDF、SI 和历史图片不删除；每次实际写入追加来源和事务审计。
actor 来自服务器验证的 OAuth/key 身份，客户端不能自报账号；工作台 Cookie/CSRF 保持原规则，MCP 不伪造工作台会话。
无任意 URL/SQL/文件路径工具，无删除或清库工具，无原生任务派发工具；外部 AI 不获得 SSH 或服务器凭据。

## 建议首次提示词

> 使用 Literature AI 查询《论文标题》。先核对 paper_id、正文/SI、已有成果、具体缺项和当前占用；先只读汇报。经我选定本篇范围后，由你阅读原文并通过新版工具保存结果，不启动其他 AI，不自动处理下一篇。缺失或看不清保留为空，保留来源与原始精度，最后读回并报告已保存和未完成范围。

代码、GitHub 发布、真实域名 MCP 协议和网页版 ChatGPT/Claude 操作须分别验收；不能只凭源码或 OAuth discovery 声称已接通。

### 2026-10-26 B批交付索引源码候选（未部署）

现有 `get_paper_workspace` 可选 `view=delivery|delivery_checkpoints|delivery_gaps|delivery_receipts|delivery_jobs`，直接复用正式HTTP `/api/rebuild/papers/{paper_id}/delivery-package` 的同一只读投影。默认compact及旧参数保持兼容；delivery分页使用交付revision，旧视图仍用科学output_revision。三层状态、缺项、来源与原始占用保持一致，不触发模型、续接或解占用。本改动仅业务只读视图，不安装平台、不改变鉴权。真实Streamable transport和并发的实际结果见本轮独立交付回执，不以本文声称上线。
