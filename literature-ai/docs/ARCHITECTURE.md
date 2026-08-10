# Literature AI 当前架构

本文只描述当前长期有效的模块边界。阶段计划、历史验收数字和一次性迁移记录留在 `plans/`、`audits/`，不在这里重复。

## 运行拓扑

- `owner-gateway:8000`：Owner API、页面与带认证的 MCP 入口。
- `share-gateway:8080`：只读分享面。
- `backend`：FastAPI API、业务服务、数据库会话与 MCP server。
- `worker` / `worker-pdf`：异步通用任务和 PDF 任务。
- `postgres` + pgvector：唯一业务真源。
- `redis`：任务队列。
- `minio`：对象存储。
- `grobid`：PDF 结构解析依赖。

Compose 中核心依赖均有健康检查。后端和 worker 等待依赖健康后启动；网关等待后端健康。不要通过删除 volume 处理普通启动问题。

## 后端边界

- `app/api/`：HTTP 路由、请求校验和响应组装；不承载大型领域算法。
- `app/services/`：工作流编排和领域服务。`ai_verification_service.py` 负责专用单 AI 验收及确定性门禁；`evidence_page_recovery.py` 负责从真实 PDF 恢复页证据；`section_page_fragment_materialization_service.py` 只将通过服务端复核的页片段物化为未审核候选。`app/rag/multi_paper_evidence_plan.py` 生成有界、只读的多论文证据计划。跨 DFT/图表 bundle 的公共逻辑在 `review_bundle_shared.py`，人工复核进度兼容逻辑在 `manual_review_progress.py`。
- `app/db/`：模型、会话和启动初始化。`bootstrap.py` 负责启动锁与结果契约；初始化只有完全成功后才缓存 URL。数据库 bootstrap/DDL 只由 backend lifespan 执行，worker 在 Compose 中等待 backend 健康，Celery import 不执行迁移。
- `app/mcp/`：认证后的 MCP 工具面。HTTP MCP 必须使用服务配置中的 Bearer key。
- `app/security/`、`app/utils/`：安全边界与无状态公共函数。

同步导入的事务规则是：业务写入失败后先 `rollback()`，重新读取 workflow job，记录原始错误，再返回 API 错误。不要在 failed transaction 上继续查询或提交。

## 内容审核与写作边界

- 普通 `import_analysis` 只导入候选或审核意见。非 DFT 候选保留为 `authenticated_human_review_required` / `no_ai_overwrite`，不会覆盖既有正式对象；DFT `new_candidate` 可被受控物化，但仍是未验证候选。
- 自动权威验收仅由具有 `ai_verify_content` 的身份调用 `get_ai_verification_tasks` 与 `submit_ai_verification_batch`。该路径可写 `ai_verified`，不能伪造人工 `verified`；确定性门禁仍无法解决的对象才进入 Owner-session 异常处理。
- 对非受信任的 `propose_correction` 直接写入，服务端强制模块锁的范围仅为顶层 `abstract`，以及结构化 `sections`、`mechanism_claims`、`writing_cards`。`title`、`year`、`journal`、`authors` 等其他允许字段不应描述为服务端必然强制锁；表格和图像遵守各自专用工具的 capability/evidence 契约。锁不把 `import_analysis` 变成非 DFT 覆盖通道。
- Content web review bundle v1 已废弃。v2 只校验 proposal，没有直接 apply 路径；history 展示生命周期。retention 默认 dry-run，候选删除的共同安全前提是状态为 `generated`/`stale`、`proposal_payload IS NULL` 且无本地结果，并始终排除 `exclude_bundle_ids`（包括当前 active bundle）。duplicate 路径为每组保留一个可复用 keeper、删除其余重复包且不要求年龄阈值；expired 路径则要求达到 `older_than_days` 且当前 scope fingerprint 与包 snapshot 不同。`limit=100` 是单次扫描、处理及删除上限，不是保留数量。
- AI Writer 只调用 `/api/content-knowledge/writing-plan`，每批最多 10 篇；`can_use_for_writing` 与 `can_use_for_citation` 分开，blocked 内容不进入上下文，也不调用 `/api/writer/draft`。

## 前端边界

前端是静态多页面应用。页面 HTML 保留结构，页面级 CSS/JS 放在同目录的 `page.css` / `page.js`；共享样式和导航位于 `frontend/shared/`。Review Center 是正式的单篇 AI 提示词入口，图表流程默认一次覆盖主文全部图表与 DFT 相关 SI 图表，避免拆成多个互相阻塞的入口。

## 数据与产物边界

- PostgreSQL 是事实源；测试不得使用真实业务 schema。
- `storage/`、`data/`、`outputs/tmp/`、`outputs/exports/`、`test-results/`、`.pytest_cache/` 是运行产物。
- 数据库清理备份和大型恢复 JSON 放在根目录 `local/backups/`，不进入 Git。
- `deliverables/` 只放明确需要版本化的交付快照。

## 验证

从仓库根目录运行：

```bash
python scripts/verify.py fast
python scripts/verify.py full
```

后端数据库测试自动创建 `pytest_<uuid>` schema 并在结束后删除。前端 Playwright 在 `127.0.0.1:4173` 启动独立静态服务，`reuseExistingServer=false`，因此不会把正在运行的 Owner 网关误当成测试服务。

## 仍需持续小步拆分的热点

`app/db/session.py` 的历史迁移编排、两个 review bundle service、`paper_workbench_service.py`、`paper_query.py` 和部分前端 `page.js` 仍然偏大。后续拆分应遵循：一次只移动一个稳定职责，保留兼容入口，先补回归再移动，不同时改变数据库语义和 API 契约。
