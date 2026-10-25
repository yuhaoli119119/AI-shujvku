# 提取写入、结果质量与独立验收合同

2026-10-17：本文描述源码合同，实际部署能力以明确 commit、发布回执和真实页面验收为准。原生调度、独立验收授权和队列启用仍需单独接入验收，不因代码存在而自动启用。旧暂停批次与 SI heartbeat 保持原状态。2026-10-23 用户已授权普通解析修复与无占用最小真实样本验证，按 [普通解析](ORDINARY_PARSING.md) 执行；不得因此重启整库批处理或将普通解析升级为科学 accepted。

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

## 表格表示完整性合同（实际版本按发布回执）

原始 `context_text` 仍可保存、显示和精确复制，但不构成完整表格表示。冻结时这类表格明确为 `partial` / `table_representation_incomplete`；独立 reviewer 不能将未满足此门禁的冻结直接置为 `accepted`。结构化行列或具有表头、分隔行及一致列数正文的 markdown 仅通过表示检查，不代替科学正确性、来源和独立验收。

正式表示也必须通过独立来源门禁：注册 file 属于本篇，实际 PDF 哈希匹配，且页集合非空、每项为合法物理页。缺 file、跨篇或越界页明确为 `table_source_unverified` / partial。正式表格可以没有表号；不能因为缺表号而跳过文件归属和真实页范围校验。

旧 `paper_tables.markdown_content` 无需补造历史来源或审计。使用它通过完整性检查前，必须核实同一所属 paper、注册 file、实际 PDF 哈希、表号及物理页；SI 仅允许明确 SI 关系内且 PDF 哈希匹配的文献。跨文件同号、无关文献、缺页、多重匹配和未知来源保留 partial。页面剩余 legacy 表格也进入读取范围，不能借用邻表正文补齐。

冻结 manifest 新增实际 legacy 全记录摘要、合法 SI/PDF 范围与表示状态。记录正文、表题、页码、合法关系或来源身份变化后旧冻结失效。分段读取另将实际阅读 PDF 身份及章节来源事件纳入修订依赖，已有旧冻结读取时可能提示重新核验，不改写旧验收或科学结论。不迁移、不重写已存科学记录，不从历史输出自动导入 accepted。

## 分段读取与已存结果质量检查

工作包默认 full 保持旧字段；新增 compact 首包及 rows/assets/sections/legacy_tables 有界分页（rows_limit/rows_offset，每页最多500，建议50）。分页链接绑定 expected_revision，数据库以同请求 REPEATABLE READ / READ ONLY 保证本页内容和修订一致；修订变化拒绝混合。章节分段保留完整原文及该节导入事件，修改章节前仍按既有 sections 接口核边界更正历史。不存在跨请求省略修订校验的全局快照缓存。

行页批量读取值/来源/论文身份，并复用已读值计算原 identity_revision；显式 canonical row_key 导入先查目标，回退路径批量加载身份维度，仍保留别名歧义、冲突、审计、幂等与完整证据。相同工作包请求只构建一次冻结快照；独立验收状态复用该快照，不削弱物理源门禁。

`GET quality` 是已存结果检查，不执行原生任务、不写科学记录、不生成缺失科学内容。它批量检查 row/value/source/paper/file/asset 关联、首选来源、缺失理由、非有限数值、估计依据、图与子图归属、物理页与章节导入原文一致性，返回 error/warning/unknown。未知历史章节的文件范围另列，不要求补造历史审计或科学必填字段。全文/SI 完整覆盖仍须有真实物理页证据；页数、对象数、HTTP200 或模型 completed 都不能代替。

2026-10-18 C4 存量图资产独立机械诊断（部署状态以发布回执为准，不代表正式科学验收）：`integrity_report` / `quality_report` 默认不请求新检查，始终在独立 `asset_mechanics` 返回 `not_requested` 与未查数量；原 status/counts/issues 只表示原有关系/来源完整性，不能解释成几何、图片或科学完整已验。正式验收原调用和gates不变。显式只读入口：`GET /api/rebuild/papers/{paper_id}/quality?include_asset_mechanics=true&asset_offset=0&asset_limit=32`；结果有自己的 status/counts/issues，坏例 errors、预算或未知格式 unknown，不混入原完整性/正式接受规则。按asset ID顺序检查指定切片（最多32条），返回 selected_asset_ids、前后/切片外未查数与 next_asset_offset；续查携带 `expected_revision=output_revision`，数据库变更拒绝混页，不提供跨请求物理文件快照保证。原 offset/limit 只分页旧问题，不能作为资产续查。UI暂未接入显式诊断，按 producer_contract 直接调用。

机械诊断仅对明确 `units=normalized_page_fraction` 的 x0/y0/x1/y1 或 `regions[].bbox=[x0,y0,x1,y1]` 检查有限数、0..1边界和严格顺序；未知/历史单位和未识别格式 unknown，不转换。每bbox最多100个region。有image_path检查storage根、存在、可解码和正尺寸，区分越根、missing、corrupt与unknown；无图图像草稿unknown，纯文本表不要求图片/bbox，保存合同不变。每页最多32次图片尝试，每图16MiB/1600万像素，累计64MiB/3200万像素；超额、多帧或非PNG/JPEG/WEBP/TIFF/GIF/BMP为unknown。256项LRU绑定路径/dev/inode/size/mtime/ctime/像素余量，读中变化unknown。切片通过仅适用于列明资产，切片外及预算未查均未核实；没有全库通过或正式验收含义。既有全篇关联查询/源文件哈希与页数成本未改；无PDF渲染、目录遍历、重新裁图或科学写。坐标和尺寸有效不能证明轴/图例/图体完整，已知Figure 4d、SI Figure S2科学裁切坏例不能由此发现。

已知错误或未核实来源使新冻结保持 partial，不能直接 accepted；warning 不自动裁决科学冲突。章节原文使用到的导入/边界更正来源事件也进入 manifest，避免来源事件变化不使冻结失效。旧验收记录不改写，读取时按当前依赖提示重新核验。质量清单可按修订分页，界面仅由用户主动读取，不新增轮询或任务。

生产者须消费 producer_contract：图/子图同文件真实页，表格 table_data/table_markdown + table_sources（context_text仅草稿），章节按既有完整原文/独立解释/同文件哈希合同，值按精确身份与来源保存，未报告项诚实 missing_reason。统一输出只是机械合同，科学提取仍暂停；不自动转换历史 raw 表格、不双写 paper_tables、不扩大并发或接入队列/authority。

旧正确表格正文可从 legacy_tables 分页读取：仅限 manifest 中的同篇/明确 SI 关系范围，原 markdown 原样返回，来源仍按 quality 检查；不会把相邻表/无关篇混入或自动复制回资产。templates_endpoint 提供模板，read_endpoints 指向已有不可变文本/页面缓存。

质量接口的顶层 counts/issues_total 统一包含图关联、待结构化表格、文件覆盖未知和历史章节文件范围未知；dimensions 分项列示。A077 的 text-only 表不能呈现为顶层绿通过；B006 的正确旧 markdown 可表示完整，但无当前修订物理页覆盖声明/历史章节来源未知仍诚实提示。unknown 不等于不存在或原文未报告，也不要求补造历史审计。

章节来源按现行 RebuildSectionInput 合同重新核每页非空原句/定位与完整页集合，并核 page_basis、登记原文件大小/哈希、实际阅读 PDF 哈希（含既有 DOCX 派生 PDF）。实际引文的科学支持关系仍由独立原文复核，不自动裁决。旧表分页逐条返回 source_identity；仅列为冻结依赖不代表来源已核实或可用于补齐另一张表。

章节分页仅在原文与导入事件正文完全相等时去重：items.text 保存原文，source_import_event.section 不重复拷贝相同 text；如两者冲突则完整保留两份原文，不省略证据。source_event_projection 明示如何以 item.text 重建源事件快照并核 content_sha256，其余引文、定位和中文解释均保留，不截断科学正文。

## 2026-10-26 C源码候选：对象版本退修与独审复核（未部署）

新freeze在既有科学snapshot之外记录`object_versions`（对象kind、稳定ID、规范保存图像hash、已有来源file_ids；阅读模块abstract/guide分别记录）。旧freeze缺字段仍按原合同可读，不追造历史版本。新freeze在正式review决策前核对对象版本，阅读变化虽不改变原科学revision仍拒绝陈旧accepted；review_incomplete可如实记录。

review请求可选`failed_items`：`issue_id,kind,object_id,object_version,source_file_ids,required_change`。对象必须属于该冻结、当前版本一致，已提供来源必须属于对象已存绑定与冻结文件集合。accepted不得包含failed_items。旧请求未发送新字段时保留原幂等指纹，旧回执不改写。

`resolved_items`绑定`prior_freeze_id,issue_id,kind,object_id,prior_object_version,current_object_version,correction_event_id,note`，章节替换可额外给`current_object_id`。仅独立reviewer可记录：服务端核对原独审问题、原版本、正式修正before/after及其成功事务receipt、当前独立读取及新冻结版本。mutations用原before/after作写前保全；阅读复用beginner_zh_v1专用原before/after/保存receipt；来源化章节复用真实导入hash、section_boundary_corrected声明和专用correction receipt，明确新章节ID，保留旧章节字节，不拿新文本冒充覆盖旧记录。

同账号review仍必须原服务端签发的独立原生授权，禁止修正者自闭合。阅读事件没有task_id时，服务端可用原保存receipt.job_id→已存WorkflowJob的paper/actor/thread与授权核实过的writer thread绑定；不从客户端补身份。旧阅读job缺actor/thread或来源章节专用事件缺原生身份时，默认同账号关闭保持fail-closed；只有下述私有总指挥对精确事件提供签名归属见证才可受控复核。独立不同认证主体仍可走专用证据链；相关真实生产闭环仍需独审。

历史结构化问题从immutable independent_review审计receipt投影，新的freeze不隐藏旧open问题；对象变化标需独立复核，不自动关闭。新accepted若遗漏任何未关闭问题则拒绝。交付包summary返回`independent_issue_summary`，gaps返回精确未关闭问题、原对象版本及当前新旧状态。旧review没有结构化项时保持unknown，不把旧message自动解析成问题或伪造closed状态。已解决对象再变化重新标记需核实，不自动继承通过。

恢复继续复用原稳定request_id与持久receipt；未知ACK不重复create。隔离测试允许新进程/Session读回同一回执及同actor work身份，不派模型，不释放未知占用。本候选测试、独立快照、最终提交与生产/视觉证据各自报告，不将文档作为上线证明。


C3候选受控归属见证：服务端本地`issue_reviewer_authorization`可选成对提供`correction_event_ids`、`writer_ownership_evidence`（受控evidence_root内的普通非symlink文件）及`writer_ownership_sha256`。不是HTTP输入，也不能用工作台会话密钥签发；必须总指挥持独立私有key调用，并沿用真实native readback核对writer/reviewer不同、项目与turn。

证据JSON格式`coordinator_correction_witness_v1`，字段paper_id、writer_thread_id、events。每项严格为event_id、event_sha256、prior_freeze_id、issue_id。签发前后均核对原事件同paper/actor、原始完整存储内容hash（不二次脱敏掩盖科学键变化）、原问题→冻结时间区间和真实对应成功receipt；原问题及目标不得借用。claims签入精确事件集合、内容hash和证据artifact相对路径/hash；复核时再次验证原字节、事件与回执，不允许普通grant认领其它历史事件。

签名结果明确标记`current_coordinator_witness_not_historical_event_identity`，是当前总指挥核实归属的见证，不回填旧事件task_id、不改旧文件/章节/审计。无见证、错误hash、证据变化、不同actor/篇、时间错位或原回执不符均拒绝。生产authority未在本轮配置，隔离fixture签发不代表生产授权已接入。
