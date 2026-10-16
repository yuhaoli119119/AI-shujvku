# 同事实值合并合同

正式入口与最新机器可读合同位于登录后的 `GET /api/rebuild/papers/{paper_id}/ai-work-package` 的 `value_dedup_contract`。这项功能不判断科学等价；代码发布不代表科学验收通过或批准删除。来源修正 PUT 的原有身份字段边界保持不变。

1. 独立科学复核明确每个 same-paper pair 的 canonical/duplicate value UUID、same_fact=true、reason 和 identity_assertion。identity_assertion 必须说明具体同一事实，包括条件/field 表述差异。材料、载体、位点类型及位点、构型、材料族、反应、实验/DFT 类型必须严格相同。数值、单位、值类型等不同则拒绝；相同数字本身不是去重授权。canonical 必须是创建时间最早的值。
2. 登录且通过 Origin/CSRF 的 `POST /api/rebuild/papers/{paper_id}/value-dedup/preview` 接收 `{pairs:[{keep_value_id,duplicate_value_id,same_fact,reason,identity_assertion}]}`，只读返回完整 canonical/retired row/value 及全部 source 前后影像、`delete_value_ids`、`delete_row_ids`、`move_source_ids`、数量与稳定 `plan_revision`。NC 等身份有歧义的记录不得纳入。完整外部引用检查失败或来源 key 碰撞返回 409，不会丢来源。
3. 将预览中的逐 UUID 删除清单、数量、影响与恢复方法交给用户，取得明确删除批准。不得把预览或发布当成批准。
4. `POST .../value-dedup/apply` 在原 pairs 外要求 `expected_plan_revision`、精确且无重复的 `confirm_delete_value_ids` / `confirm_delete_row_ids` / `expected_move_source_ids`、UUID `request_id`、非空 `reason`。必须保留原 payload 以便重试；reason 应记录对应用户批准。actor 只从可信登录会话取得，不能由请求指定。
5. apply 锁 paper 与同篇全部 row/value/source；为防止 JSON/FK/其他引用并发插入，对当前 schema 的公共业务表短暂获取事务级 SHARE ROW EXCLUSIVE NOWAIT 锁。锁冲突直接 409，不能在活跃科学写窗口运行。锁内重新预演并比较 revision、精确清单与数量；任何失败整体回滚。引用扫描涵盖所有表整行 JSON（UUID、preferred、conflict、analysis、FK、文本），保守拒绝无法安全维持的引用，仅跳过本次退休记录、改挂 source 的旧父指针及既有 merge_history 归档影像。范围不是任意删除接口。
6. 所有 source UUID、身份字段与 key 原样保留，只同步改挂 value_id/row_id；来源 key 重新计算并验证一致，不含父 ID。canonical 的 raw、精度、单位、value_type、preferred source 与旧 conflict/history 保留。只删除确认清单中的 duplicate value 和因此变空的 row，不进行 cascade。
7. 每个 canonical 的 `conflict.merge_history` 追加完整批次预览/前后影像、所有退休值/行的旧历史、原始 raw（如 0.40 与 0.4）、可信 actor/time/reason/request/revision 与回执。不伪造 value-conflict，不用 legacy 隐藏重复行。通过登录工作包的 canonical value.conflict 可读回。可按 beforeimage 原 UUID 恢复行和值，再将来源指针改回，并恢复 canonical 原 conflict；恢复是另行授权的事务性维护，不在本接口自动执行。
8. request_id 在合并请求中全局唯一，同 actor+payload 重试返回原回执且零写；异 actor/payload 返回 409。结果不确定时先认证 GET 工作包核对 merge_history/request_id，不能新建请求盲重试。

生产验证只允许认证 GET 与 POST preview；禁止用任何 apply（包括故意无效 apply）探测生产。成功、CAS、并发、引用、回滚与鉴权写路径测试仅在隔离 PostgreSQL 测试库中执行。
