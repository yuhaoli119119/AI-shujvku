# AI 提取章节边界纠正历史

本契约只表达 AI 提取边界的纠正，不裁决原文科学冲突，不授予科学验收。既有章节及 `section_imported` 审计完整保留；不迁移、不初始化、不改模型。没有显式声明的 legacy/raw 章节照常展示。`conflicts_with_section_ids` 和中文解释不会触发历史化。

同篇 owner 在占用串行写窗口内，先备份，再由独立安排的 DS metadata 任务调用。代码任务和部署均不得替科学 owner 自动标记生产章节。

`POST /api/rebuild/papers/{paper_id}/sections/corrections` 使用已登录工作台 session、同源请求及 `X-CSRF-Token`，MCP key 不能代替登录。请求应先保存完整 JSON，重试使用原字节。

```json
{
  "request_id": "stable-boundary-declaration-id",
  "corrections": [{
    "kind": "extraction_boundary",
    "file_id": "registered-same-paper-file-uuid",
    "source_sha256": "64-lowercase-hex-original-file-hash",
    "old_section": {
      "section_id": "existing-old-section-uuid",
      "content_sha256": "GET-sections-provenance.content_sha256"
    },
    "new_sections": [{
      "section_id": "existing-corrected-section-uuid",
      "content_sha256": "GET-sections-provenance.content_sha256"
    }],
    "reason": "Explain the extraction boundary error using the physical source headings.",
    "sources": [{
      "page_number": 1,
      "locator": "Printed heading and paragraph boundary",
      "quote": "Original evidence quote"
    }]
  }]
}
```

示例中的 UUID/hash 是契约占位说明，不能直接提交。old/new 都必须是已经导入、有完整来源审计的同篇、同文件章节；不猜 legacy 文件归属。`content_sha256` 是完整章节导入 snapshot 的摘要，包含原文、标题、页码、中文解释、来源和冲突关系，服务同时核对原有 `PaperSection` 字段。文件原始 hash/bytes、阅读 PDF hash、物理页范围须匹配。每个新章须与旧边界有物理页交集，纠正理由的引用页须同时位于旧章及至少一个新章；具体引文语义和标题边界仍须 DS 独立核查，代码不认证科学内容。

每项表示一个旧章到一个或多个已保存纠正章。多旧可以共享新章。一次请求不能把另一个本次旧章或已标历史章作为新目标，避免环及把历史指向历史。日后可另声明新章到下一版章，形成可追溯历史链。已有旧章声明不能被另一理由或目标集合覆盖；发现新的纠正需求须保留声明，追加新版本章节和新关系。

仅追加 `AuditLog`：`source=rebuild_section_corrections_v1`、`action=section_boundary_corrected`、`target_type=paper_section`、`target_id=旧章 UUID`。payload 保存 `declaration`、声明摘要、阅读 PDF hash、authenticated actor、request ID/hash。同一事务追加 `section_correction_request` 回执。锁定本篇 Paper 行串行化并发调用；旧原文、导入审计、文件、图表和值不 UPDATE/DELETE。

- 同 request ID / 同完整 body：`created=0, replayed=true`，零新事件、零新回执。
- 同 request ID / 改 body：409 `section_correction_request_id_reused`。
- 新 request ID / 同声明：零新纠正事件，追加一个请求回执。
- 改已有旧章声明：409 `section_correction_relation_conflict`。
- 摘要不符、来源变更或新目标已为历史：409；跨篇/跨文件、未知来源或非法来源关系：400；非法 kind、空理由/引文或未知字段：422；无登录：401；CSRF/跨源不符：403。
- 任一校验/事件/回执失败：整批回滚，无半个声明。

GET sections、工作包和成熟详情共享只读投影，增加 `display_status` 与 `correction_history`。只有合法版本、类型、目标和摘要匹配的显式纠正事件，才使旧章成为 `extraction_boundary_history`；其余为 `current`。关系双方均返回原始理由、引用、旧新目标摘要、审计 ID、actor、request、来源和阅读 PDF hash。读取不会写库，损坏/失配声明不历史化。

成熟详情页保留当前章、中文解读、来源卡和真实 PDF 链接；旧章进入明确标记的历史组，展开可读全部原文，复制直接使用原 text，关系可跳转旧新章，纠正依据可点到物理页。通用科学冲突各候选保留，独立核验提示继续展示。代码发布本身不会给任何生产章节生成声明；真实历史页面验收须等待 owner DS 的合法声明及独立读回、实际 Chromium 内容和 PDF 页码验证。

回滚代码至 `55dc57a` 仍保持只读启动 gate，并保留全部纠正审计。旧代码会再次普通展示所有章节（历史展示暂不可用），不会恢复 init/migration 或删除关系；升级回来可重新投影同一关系。
