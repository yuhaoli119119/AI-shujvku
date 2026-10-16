# 提取写入、独立验收与持久队列（源码候选）

2026-10-17：本文描述本次源码合同和隔离测试，不代表已部署。发布、真实 Chromium 和原生 IPC 验收由独立总指挥执行。生产科学任务、文献解析和 SI heartbeat 保持暂停；不要为了试接口向生产创建任务或回写科学记录。

## 表格正文与分页

当前资产继续存于 `rebuild_visual_assets`，不双写 `paper_tables`。正规正文使用 `provenance.table_data`（`columns: string[]`、`rows: (string|null)[][]`、`footnotes: string[]`）或 `provenance.table_markdown`；对应 `table_sources` 保存自身 `file_id`、真实物理页、`role=caption/body/footnote`、`locator` 和可选原句/行列。正文与表题分在 71/72 页时分别记录，资产 `page_numbers` 保留两页。新正规正文校验文件归属、原文件哈希、阅读 PDF、物理页范围、矩形宽度和来源位置。正文不可用时不猜行列。

工作包和图表 API 返回统一 `table_content`。详情页优先结构化正文、资产 markdown、旧表格 markdown，再回退 `context_text` 的安全原样文本。复制结构化表得到保留字符串和空值的 TSV；复制 markdown/原样文本保留原始字节表示的文本，不把 `s`、`1.00s` 或缺失转换成数字。跨页分段保留各段，不自动去掉重复科学行。

`GET /api/rebuild/papers/{paper_id}/ai-work-package?rows_limit=500&rows_offset=0` 返回 `rows.total/limit/offset/has_more/next_offset/next_page` 和 `output_revision`。继续读取 `next_page`；不能把前 500 行当全量。同一分页读取期间 revision 改变必须重新做只读收集，不能重建已派发任务。

## 写入与审计

资产批导入、资产更新、裁图及数据行导入使用工作台会话和 CSRF，actor 来自认证会话。旧请求体仍可使用；建议发送稳定 `request_id` / `task_id` 或 `X-Request-ID` / `X-Task-ID`。任务 ID 是追踪信息，不能替代认证 actor。

同文献串行锁定，实际数据与成功审计/回执同事务提交。每个成功批项记录完整 before/after、变更字段和批项序号；同批多次改同一对象仍保留各步。失败项单独返回，成功项的审计 ID 可对应到结果。回滚不能留下成功回执。审计中的凭据字段脱敏；不追补虚构历史审计。

同文献、同操作、同 request_id 和 actor 的相同请求重放原回执；内容或 actor 改变返回 409。回执描述原次操作，读取当前对象才能确认当前状态。无 request_id 的旧请求由服务端分配并返回 ID；未知结果应使用返回的 ID 或预先保存的 ID 核对，不盲目重发。裁图按 PNG 内容哈希另存，数据库回滚不覆盖旧图片；失败可能留下未引用文件，须另外列出清单并按删除门禁处理，代码不会自动清理。

## 完成门禁

`parsed` 仅表示解析投影，始终不等于 `accepted`。没有正式冻结和独立回执时状态是 `pending`，不会从旧 outputs 或历史计数导入通过。

- `GET /api/rebuild/papers/{paper_id}/acceptance`：当前验收投影、合同和当前输出 revision。
- `POST .../acceptance/freeze`：稳定 request_id、writer task_id、各注册文件的 source/read-PDF 哈希、已覆盖物理页和剩余原因。冻结实际数据库输出 manifest，保存提取与规则检查回执。不修改科学值、旧结论或解析状态。
- `POST .../acceptance/{freeze_id}/reviews`：独立 reviewer_task_id、预期冻结 revision、decision、实际复核的文件和证据。decision 包括 `accepted/needsfix/partial/review_incomplete`。无法读取证据可诚实报 `review_incomplete`，不伪造 coverage。

`accepted` 要求所有冻结文件完整覆盖、规则检查通过、没有未裁决来源冲突、输出和物理来源版本未变、独立 reviewer 的证据。writer 不能自 accept。同一工作台账号可提交不同原生任务的独立验收，但必须持有独立协调者预先签发的受控授权；普通登录不能通过自填 task_id 获得权限。`app.services.rebuild_task_authorization.issue_reviewer_authorization` 是服务端本地协调适配器，没有共享登录可调用的 HTTP 签发入口。协调者提供单独的私有 authority key（配置路径 `LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE`，不使用工作台密码或会话签名密钥），核对 writer/reviewer 的不同原生 thread handle、项目 cwd、原生 turn，并将 owner、冻结 revision、task、证据目录和有效期签入授权。工作台只提交短期 `X-Reviewer-Authorization`；原权限密钥不交给 reviewer，也不进入报告。队列任务还要与服务端持久 handle 记录一致。不同认证主体仍可独立复核，不能复用 writer 的任务 ID。

当前生产未配置/签发此 authority，不能假装已集成：真实同账号工作流须由总指挥批准配置并验证服务端本地签发适配器，**不要求用户另建工作台账号**。无有效授权返回明确门禁未集成/授权缺失；不能把旧 shared-account 任务自动通过。已通过回执不可覆盖；后续修正另冻结新版本。文件、关联 SI 或输出变化使当前投影回到 `review_incomplete`，历史回执保留。

## 持久队列与无进展保护

队列复用 `workflow_jobs`，没有自动迁移、启动 hook 或后台无限循环。`LITAI_REBUILD_QUEUE_ENABLED=false`，缺少控制记录时默认暂停。生产启用需要另外授权和独立验收；部署本版本也不会自动创建队列。

接口：`GET /api/rebuild/queue`、`PUT /queue/control`、`POST /papers/{paper_id}/queue`、`POST /queue/claim`、`POST /queue/{job_id}/dispatch-intent`、`POST /queue/{job_id}/readback`。状态读取不创建任务；界面提供暂停和待办。所有修改保持认证与 CSRF。执行适配器为 `app.services.rebuild_queue_native.dispatch_once`，只能消费已提交的同一派发意图，不能当作可自动重试的创建接口。

全局调度锁协调同一 paper_id 的 writer/reviewer 租约，同时考虑现有提取 writer。默认 writer/reviewer 各一槽、总槽一，不扩大默认并行。不同文献只有明确配置更多总槽后才能流水。提取/验收独立 token 预留、总预留和验收积压限制都在派发前检查；目标模式传递显式 tokenBudget 并核对 set/get 回执，运行时实际预算停止仍须原生验收。原生用量只采纳匹配线程的 `tokensUsed`，缺失显示未知，不以零代替。

dispatch intent 和 attempt token 先持久化，再执行现有原生 IPC 桥；每个请求与匹配回执先保存才进入下一步。未知结果/失联/租约过期保留占用，核对同一线程，不重建。原生 readback 证明同一线程仍在工作时可续租；目标自动续轮必须在回读中同时包含最初 turn 和当前 turn 才接受关联，不能借另一任务。

writer 无进展按真实语义输出、经过验证的覆盖和回执变化判断；reviewer 另按服务器核对过的只读复核证据判断，不要求写科学值。`POST .../acceptance/{freeze_id}/review-progress` 需要可信 task 授权、项目内报告路径/hash、冻结 revision，并核对每个物理页的原句、file/hash/页码和复核类别。server 在审计中保存验证过的 manifest/observation；同一 file/page/check 重复报告、不同随机文件名或消息不增加进展。reviewer 使用较长的一小时无新证据窗口，writer 为十五分钟；仍保留有限重复空完成观察。

版本号、重复 checkpoint、HTTP 200 和 turn completed 不是进展。空完成或停滞达到有限观察次数后转 `needs_attention`、保存诊断、停止应用侧自动回读；不会无限重派或换模型。A077 的空 completed 根因仍未确定。413 区分上下文分块/图片数量与渲染尺寸；只读 429 最多三次有限退避；未知写结果必须先核对回执。

**跨项目边界**：复用现有 8214 原生 IPC，不新起 app-server、不 cron、不用 shell 轮询冒充 native。自动事件透传没有经过本项目验证，`automatic_event_execution=false`。fallback 是协调者自己的原生 heartbeat 显式调用同一任务的 claim/readback 适配器；本次不创建或恢复生产 heartbeat。另有 `LITAI_REBUILD_NATIVE_STOP_ENABLED=false` 控制实际原生停止，默认仅检测告警，界面明确提示“原生任务尚未确认停止，可能仍消耗额度”。启用后，`app.services.rebuild_native_stop.protective_stop` 先持久化唯一停止意图，以现有服务端保存的 owned thread/初始 turn 核对原生 readback；暂停同一 goal，再读回自动续轮并中断当前 owned turn，最终同时核对 goal 已暂停/完成及 turn 已停止才返回 verified_stop。`POST /api/rebuild/native-attempts/{job_id}/stop` 还验证同一 attempt token。丢失回执、并发请求和服务重启均只核对同一个停止意图，不盲重发或误停别人的 handle；未知保持需要人工核对。目标的 blocked/usageLimited 不冒充持久暂停。该 adapter 已做隔离协议/事务测试，真实上游行为仍需总指挥原生验收；本次不启用、不调用生产停止接口。旧文档引用的 `scripts/literature_batch.cjs` 不在当前基线工作树；保留 batch_id 接口，不复活旧目录或未知批次。

## 统一只读工具与缓存

`app.services.literature_tools` 提供只读 PostgreSQL 快照、工作台 API 全分页收集、现有浏览器控制器状态读取。`scripts/literature_tools.py` CLI 只读，输出必须是项目 outputs 内新文件。API 登录凭据仅从本机私有文件读取，不输出、不加入任务提示词或 Git；不会关闭鉴权。现有浏览器控制器不可用时诚实失败，不新开浏览器或关闭别人的页面。

`GET /api/rebuild/files/{file_id}/text-pages`、`render-page`、`asset-inventory` 提供按原源哈希和阅读 PDF 哈希绑定的分页文本、页面渲染和图表清单。文本层为空不代表页面无内容；不自动 OCR/解析或科学裁决。图表清单另外绑定库存 revision，并支持 `asset-inventory?inventory_revision=<hash>` 读回旧清单，避免把同一来源下变动的解释当不可变。缓存只新增内容，不自动删除源文件或旧缓存。

吞吐来自正式独立 accepted 回执/观测窗口，另列返工、验收积压、预留与已暴露用量；未知保持 null，不承诺未实测倍率。


## 成本与并发边界

验收只锁当前 Paper 行，沿用图表、行、数值、来源、章节和去重服务的同篇锁顺序，不再使用全局 SHARE TABLE 锁。状态投影在 PostgreSQL 内对每个持久行 JSON 计算内建 SHA256，只传输 ID/hash，避免把全部章节/冲突正文搬回 Python；不增加扩展或迁移。PNG 哈希缓存绑定路径、device、inode、size、mtime_ns、ctime_ns；文件版本变化重新计算，正式 freeze/review 强制读物理文件。读取状态仍有随实体数增加的数据库哈希和文件 stat 成本，没有声称大规模常数时间。

事务审计在 importer 解析出本次行/资产身份后只读取其完整 before/after；不在每次请求前扫全篇。单批上限保持 200 项，值/来源按本次目标保存。现有数据 importer 为科学身份去重读取全篇行的成本仍保留，未为本次审计另加一遍全篇快照。600 资产合成夹具验证单个资产审计仅目标前后各一次；三次相同状态读取 PNG 内容只哈希一次；持有 A 篇验收锁时 B 篇可独立提交。测试时间是该隔离夹具的观察值，不外推生产倍率。
