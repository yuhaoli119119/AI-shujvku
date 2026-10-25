# 中文摘要与论文导读

文献详情默认从概览阅读；显式tab和图表定位链接保持原位置。30秒读懂、中文摘要、论文导读逐步展开；标题区只保留元信息与主操作，原有检查与验收工具在“解析详情”中保留。

摘要中文译文复用`comprehensive_analysis.abstract_zh`，原`Paper.abstract`保持不变；新导读存入`reading_guide.beginner_zh`，旧导读字段保留。中文摘要要求忠实翻译原摘要，导读另述全文信息，不将全文总结冒充摘要翻译。来源范围、文件哈希、文字可读程度和图片限制随导读保存；未读完整正文不得声明全文依据。阅读辅助不代表scientific accepted。

普通解析的同一任务在末尾可轻量生成并保存中文阅读辅助，失败不阻塞已存图表/数据，也不阻塞普通解析结束。已有文献可在详情页点击“补充中文导读”，创建持久阅读 job；已有已核实同篇原生任务时续接原对话，仅在无可复用身份的首次任务路径创建对话，不重新提取科学数值。单篇独占，旧未知写者不释放。Web直接模型调用保持关闭，复用原生派发、自动启动接认、任务读回和有限HTTP退避。

GET读取已存内容和状态，不派发；POST jobs需会话/CSRF与稳定request_id，同一请求返回原任务。未知派发不再创建；明确原生终态失败才可单独重试。保存以input_revision和阅读字段版本保护、来源绑定、稳定回执和before/after审计，保留旧数据。旧导读接口更新不会清除新命名空间。

导读内容含一句话、问题、主要发现、背景空白、目标、方案与关键对照、创新比较、结论及适用条件、局限、论证脉络、少量真实图表证据和术语。只根据当前实际文字和已有图注/解读；作者声称创新与对照支持的比较须区分，不能猜测心理或独立宣称世界首创。DOCX文字/表格可读不代表看过内嵌图片像素。

元数据原摘要为空时，服务端只在唯一正文的前五个已读物理页内，找到唯一明确Abstract段及同页Keywords/Introduction终止标记时提供回退原文；跨页、歧义或未核实文件保持待补。保存来源含正文file_id、SHA256、物理页、字符起止、原文哈希及来源版本，不覆盖Paper.abstract。

已有导读但缺中文摘要时，页面“补充中文摘要”复用原持久阅读任务流程，scope=abstract_only；同一保存接口只接收摘要及来源版本，保留beginner_zh所有键值、其他导读和旧中文译文。英文切换显示保存的正文摘要原文，来源链接定位原PDF物理页。历史导读缺项保留当时记录，不因补摘要清除旧科学/来源疑点。

### 出版社版本说明与摘要纠正

明确的 PDF 预出版版本说明不作为科研摘要。元数据本身保留；只有已保存译文、原文、来源和译文指纹均能对应这类说明时，才允许通过原有“单独补充摘要”任务纠正，并保留旧译文和来源历史。已正确保存的摘要继续保护。

正文回退要求唯一 Abstract 起点和 Keywords/Introduction 终点。允许相邻两页的明确连续段落，保留每页文字定位、原件哈希和来源版本；未知页眉、段落边界或多个摘要不猜测。中文内容只翻译选定段落，不以全文总结代替。页面显示实际页范围，可打开对应原件并切换原文。补充摘要不改已有导读、英文元数据或科学资料。

### 同篇验收后的受控续接

阅读 jobs 可选 `source_job_id`，只接受服务器已保存的同篇正式任务或服务器登记的验收线程。不能传裸 thread_id 声明归属。省略时自动选取本篇最新可追溯原生任务；已有身份但忙碌、模型不符、原 turn/goal 不明时拒绝续接，不另建对话。DS4.1Flash、never、完整权限在原线程恢复时再次核验；不改用户全局模型。

本批独立验收通过 `python3 scripts/register_reading_thread.py --paper PAPER_UUID --actor 登录账号` 在 master 登记，先备份本篇，再核对固定总指挥 STATE 的 `guide_wait_same_thread`、无未知占用、同篇 dispatch receipt、原生 rollout、末次验收 turn/终态与实际模型权限。`--check` 只核验本地证据。已提前归档的原线程可恢复后续接，不能借归档另建任务。登记不派发、不改原科学任务的 thread 身份。

正式请求：`POST /api/rebuild/papers/PAPER_UUID/reading-assistance/jobs`，JSON 为 `{"request_id":"稳定请求ID","source_job_id":"登记返回的source_job_id","scope":"full","retry":false}`。保存与读回合同不变，响应增加原 thread_id、新 turn_id 和 source_job_id。相同请求重放返回同一 job，未知 RPC 只读回原句柄，不自动重新发 turn。若请求刚建立，turn_id 可暂为空；不可用旧验收 turn 冒充导读完成。保存成功和原生终态分别记录。

后续同篇补缺可省略 source_job_id，沿最新已核实阅读 job 续接；原登记来源如果已被后续 turn 推进，不应强制再次指定旧 turn。整篇 accepted 与原线程终态齐全后，由原总指挥按现有归档机制归档并记录回执；失败保留原句柄，不创建替代对话。

普通解析的局部补缺也使用相同续接分支：`POST /api/rebuild/papers/PAPER_UUID/ai-extract/jobs` 仍保留原 file_ids/retry/checkpoint 合同，可选 source_job_id；默认发现本篇可复用身份时绑定原线程。补缺 job 与原 job 分开保存回执，原科学任务 thread 身份不改。存在阅读任务写者或续接未知时拒绝新写者；重放、模型与原生终态规则相同。无既有可复用身份的首次解析仍兼容原新任务路径。

### WorkBuddy图像模型额度回退与原job恢复

已核实本机图像能力的有限白名单为DS4.1Flash、GLM5.3Flash、GLM5.2；两款GLM有最小真实图像试验和完整turn_context权限读回。只在同篇原线程明确终态、实际native错误为额度耗尽或模型不可用时，按此顺序有界续接，最多向后两次。不因网络、未知结果、普通429或业务保存错误换模型，不创建替代thread/job。chosen_model、effective_model、原生尝试历史、恢复回执与实际turn_context证明分别保留；不改用户全局模型。

阅读jobs可选model，普通解析jobs亦允许这些模型；已有同篇来源默认沿原有效模型。任务进行中不换模型。`POST reading-assistance/jobs/JOB_ID/resume`（或`ai-extract/jobs/JOB_ID/resume`）使用稳定request_id，恢复同一job；已被总指挥恢复的确切原目标turn仅接认、不再派发。暂停中断仅同模型恢复，额度终态才允许向后模型切换。未知/其他写者/来源缺失均拒绝。

误停后原job被标failed但原线程已生成内容、终态明确报告reading_task_finished时，可在阅读resume请求中携带原`prepared_content`保存JSON。同一事务恢复原job并调用原保存合同，原request_id、job_id、thread_id不变；来源版本/CAS/图表归属/科学字段保护全部保留，不重新生成现有成果。请求重放校验原体指纹。服务器原生终态与保存回执各自核实后才能收尾。

`verify_paper_turn_context.py --job JOB_ID --paper PAPER_UUID`由服务器核对真实rollout、实际turn/model/cwd/full权限，再写入该job的turn_context_proof和actual_inference_verified，不接受浏览器自称证明。不读取或修改原文与科学字段。


### 无标题灰框摘要的来源回退（2026-10-25）

原科研摘要元数据和有 `Abstract` 标题的正文摘要仍优先。元数据为空或明确是出版社版本声明、正文没有 `Abstract` 起点时，context 可识别前五个物理页内唯一的灰底粗体摘要框：必须在较大题名之后、唯一的 Introduction 标题之前，与正文同左边界且比正文单栏宽，侧边真实渲染像素为灰底。普通白底段落、出版社声明、图注、多正文或多候选、旋转页及不满足边界的版式继续拒绝，不把 Introduction 内容作为摘要。

`abstract_for_translation.source` 仍为 `main_text`，附 `selection_method=shaded_abstract_box_v1`、真实 `file_id`、`source_sha256`、`physical_page`、`bbox`、`start_char/end_char`、`text_sha256` 和版面证据；revision 由服务器生成。坐标单位为 PDF point，原点在页面左上。原文保留 PDF 文字层的换行与连字符，字符范围可在 context 同页 text 中精确读回。

不新增任务或保存入口：读取最新 context 后，沿现有同篇 job 的 `abstract_only` 合同提交返回的 `input_revision`、`abstract_source_revision` 和忠实译文。保存时再次核实真实源文件字节及来源版本；不能自造 revision。合法已有译文、英文元数据、导读和科学资料的保护保持不变。不匹配这种明确版式时仍需进一步人工核对，不表示支持任意无标题摘要。
