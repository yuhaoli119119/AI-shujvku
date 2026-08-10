# Content Review 与 AI Writer 当前流程

本文集中说明 2026-08-11 的 Content Knowledge、网页审核包和 AI Writer 边界。它描述代码能力，不声明任何生产论文、具体内容批次或写作项目已经 READY。

## Content Knowledge 与权威验收

普通外部/IDE AI 通过 `import_analysis` 导入候选、审核意见或待处理对象。非 DFT 分支不会直接覆盖正式对象，而是保留候选并标记 `authenticated_human_review_required` / `no_ai_overwrite`。DFT `decision="new_candidate"` 可沿受控路径物化为未验证 `DFTResult` candidate，但不等于通过、`verified`、可导出或 ML-ready。

唯一自动权威验收路径是：

1. 专用 `ai_verify_content` 身份调用只读 `get_ai_verification_tasks`；任务读取页最大 50。
2. 同一验收身份调用 `submit_ai_verification_batch`；每批硬上限 20。
3. 服务端重跑 PDF 页、证据文本、精确定位、目标快照、版本、数值/单位与未解决冲突等确定性门禁。
4. 通过项可写 `ai_verified`，但不能写成人工 `verified`；无法确定处理的 `exception` 才进入 Owner-session 人工队列。

不使用第二模型、投票、共识或第三 AI 仲裁。对非受信任的 `propose_correction` 直接写入，服务端强制模块锁的范围仅为顶层 `abstract`，以及结构化 `sections`、`mechanism_claims`、`writing_cards`；`title`、`year`、`journal`、`authors` 等其他允许字段不属于服务端必然强制锁范围。表格和图像遵守专用工具的 capability/evidence 契约。任何锁都不能把普通 `import_analysis` 变成覆盖通道。

## Content web review bundle

- v1 已废弃，兼容端点只返回弃用错误。
- v2 是 proposal-only：网页 AI 返回 `source_identity_verified=false`、`writes_final_truth=false` 的结构化建议；没有直接 apply 端点。
- proposal 通过本地校验后仍需服务器计划的本地验证，且会重建对象、依赖、页面资产和策略指纹以拒绝 stale 或不匹配结果。
- history 提供状态、是否含 proposal、本地结果与估算 JSON 体积等生命周期信息，不改变审核状态。
- retention 默认 `dry_run=true`。所有候选删除都必须同时满足：状态为 `generated`/`stale`、SQL 中 `proposal_payload IS NULL`、没有本地结果；删除时仍会重查这些保护条件，因此并发出现 proposal 或本地结果会阻止删除。
- `exclude_bundle_ids` 始终排除，当前 active bundle 会通过该集合传入并保留。
- duplicate 清理按相同 paper、policy、模块和 snapshot 分组，保留一个当前仍可复用的 keeper；同组其余重复包不要求达到年龄阈值。
- expired 清理只处理达到 `older_than_days`，且当前 scope fingerprint 与包 snapshot 不同的非重复候选。
- `limit=100` 是本轮查询、处理和最多删除的上限，不是“保留 100 个包”或任何保留数量条件。

## AI Writer 与多论文证据计划

AI Writer 只调用只读 `/api/content-knowledge/writing-plan`；MCP 对应入口为 `plan_multi_paper_evidence`。二者按每批最多 10 篇生成有界 evidence plan，不加载所有论文全文，不写数据库，也不调用 `/api/writer/draft`。

`content_object_gate` 分开给出：

- `can_use_for_writing=true`：可以进入写作上下文；
- `can_use_for_citation=true`：可以进入正式引用计划；
- writing-only：可帮助组织文字，但不可作为正式引用；
- blocked/unreviewed：不得进入写作上下文或引用计划。

每次只使用当前 batch 的 `batch_prompt_context`。覆盖不完整时必须保留警告，不能宣称系统性、全面或穷尽性覆盖。
