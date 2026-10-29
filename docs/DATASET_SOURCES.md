# 数据集文件与数据集来源（候选）

此功能待独立验收及另行授权发布；不代表生产已迁移或入库。数据集独立于正文/SI PDF，不能作为正文解析的前置条件。

## 存储与接口

`dataset_sources` 保存 paper_id、key、版本、标题、元数据和修订；`dataset_files` 保存论文、数据集、文件名、MIME、长度、SHA-256、对象键与元数据。元数据用于记录 DOI、仓库、许可证、目标量、profile、原始/派生说明和限制。`dataset_value_sources` 追加记录来源对应的文件、哈希、表或 ZIP 成员、行/帧、列及必要的原始值；复合外键约束来源与论文同属。原来源表不新增列或改写，迁移期间旧程序的指纹保持稳定。旧来源保留原字段及原验收指纹。没有新增队列、任务或全局调度锁。

所有下列接口使用工作台会话；写入需同源和 CSRF。大文件先流式暂存、计算哈希、以内容寻址原子发布，再用短事务登记，不在上传期间持数据库事务。中断或失败可能留下未登记的内容对象；保留现场，不能因超时清理或重放未知请求。

- `GET /api/papers/{paper_id}/datasets`：列出数据集和文件。
- `POST /api/papers/{paper_id}/datasets`：同论文、key、版本的相同内容幂等；不同内容返回 409。
- `PUT /api/papers/{paper_id}/datasets/{dataset_id}/files/{sha256}?filename=...&size_bytes=...&mime_type=...`：请求体为文件原字节，服务器核对长度和 SHA-256。相同文件的元数据不一致返回 409。
- `GET /api/papers/{paper_id}/datasets/{dataset_id}/files/{file_id}`：校验并下载原文件；带限制的记录在详情页显示限制并在响应头提示。只允许已登录工作台会话，不提供公开下载入口。
- `GET /api/papers/{paper_id}/datasets/{dataset_id}/rows.csv`：导出已保存、来源指向该数据集的单元格、原始值、单位和定位信息；遵守既有 `LITAI_EXPORTS_ENABLED`，关闭时返回 403。
- 现有 `POST /api/rebuild/rows/import` 接受 `source_kind=dataset`、`dataset_file_id` 和 `dataset_locator`；先验证同篇和 SHA-256，再保存。需要沿用现有正式写入与回执合同。

故障回退窗口内，`POST /datasets` 与文件 `PUT` 通过认证和 CSRF 后返回 503 `dataset_writes_temporarily_disabled`；正式行导入、预检和来源校验对 dataset 写入整体拒绝受影响行，保留同一批次中不含 dataset 来源的其他行。列表、文件下载和 `rows.csv` 导出继续只读运行，不因暂停改变已保存内容。

`dataset_locator` 的读取与写入共用同一结构合同：`column` 必须为非空字符串，`sheet` 可缺省或为字符串，两项最多 1000 字符；`row`、`frame` 可以缺省一项，但出现的每一项必须是非布尔、非负整数，且至少存在一项；canonical JSON UTF-8 总量最多 65536 字节。质量投影对该合同任一项失败都回退为 `source_dataset_locator_invalid`，不会因为另一坐标有效而放行。

## 质量投影

`GET /api/rebuild/papers/{paper_id}/quality` 的只读完整性检查按来源自身类型投影：`source_kind=dataset` 必须存在同篇 `dataset_value_sources` 绑定和已登记 `dataset_files` 记录，定位 SHA-256 必须与文件记录一致，内容寻址对象必须位于存储根目录、大小一致且实际 SHA-256 可复核。满足这些条件时使用数据集文件身份，不要求伪填正文 PDF、图表或物理页。

绑定缺失、文件跨篇、哈希不一致、PDF 身份混用、对象缺失或损坏分别返回具体问题，不能降级为 `source_file_unknown` 后显示泛化“来源未知”。旧 PDF/表格/图表来源的生命周期和物理页检查保持不变；不迁移、不改写历史来源。质量检查通过仅表示已存来源身份与字节一致，不表示科学验收通过。

## 迁移和运行

`backend/app/migrations/dataset_sources_v1.sql` 是显式事务迁移，不在启动时自动运行。正式执行需事先备份、明确发布窗口并由独立验收，测试最终发布代码；迁移支持重入，但在已存在同名结构不兼容的数据库上不能仅凭 `IF NOT EXISTS` 推断兼容。生产执行前必须检查现有结构。

数据集记录和文件是新增成果。回滚不得删除它们或用整库旧备份覆盖升级后写入。旧代码无法显示数据集及其完整来源，直接回滚旧代码不构成无损回滚；需先准备能读取新旧成果的兼容回滚版本、暂停受影响写入并保留新写入，再由发布负责人验证。迁移失败时事务回滚；已提交的数据不应通过自动删表回滚。

`scripts/dataset_package_import.py SOURCE_DIR` 默认只读预览。`--execute --credentials PRIVATE_FILE --journal-dir OUTPUT_DIR` 才登记文件；另加 `--include-rows` 才写入源绑定行。写入前核对所有源文件和八篇论文 ID、库、已登记正文哈希及实际正文下载哈希；若 DOI 现有值非空也须匹配。记录下载日期无可靠证据时明确为空。每个写请求在项目 outputs 下先持久记录原请求，再保存回执；如请求存在但回执缺失即停止，不自动重试，需查原请求及服务端结果。严格核对下载和科学行来源读回；不得将局部失败当作完整成功。导入工具不获取网络旧版，不记录密码或会话。

U137 的 ZIP 来源 51 行按成员和源行列定位，实验/FEM 工作簿分别标记；U141 标记缺催化剂身份、8/13 特征差异和许可证待确认；U142 保留原 DOCX 的表、行、列、原值和表题，拆列注明派生；U138 只登记 profile 和文件，不灌入逐行表；四篇未取得附件只登记状态。

当前限制：数据集本身不可修改版本元数据；同身份不同内容返回冲突。旧来源修正接口尚未提供完整的数据集来源编辑合同，需通过正式受控流程另行处理，不能用 PDF 来源替换。隔离验证不代表生产升级、真实数据入库或独立验收通过。
